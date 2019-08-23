
"""Implementation of the MediaProxy dispatcher"""


import random
import signal
import cPickle as pickle
import cjson

from collections import deque
from itertools import ifilter
from time import time

from application import log
from application.process import process
from application.system import unlink
from gnutls.errors import CertificateSecurityError
from gnutls.interfaces.twisted import TLSContext
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import failure
from twisted.internet.error import ConnectionDone, TCPTimedOutError
from twisted.internet.protocol import Factory, connectionDone
from twisted.internet.defer import Deferred, DeferredList, maybeDeferred, succeed
from twisted.internet import reactor

from mediaproxy import __version__
from mediaproxy.configuration import DispatcherConfig
from mediaproxy.interfaces import opensips
from mediaproxy.scheduler import RecurrentCall, KeepRunning
from mediaproxy.tls import X509Credentials


class CommandError(Exception):
    pass


class Command(object):
    def __init__(self, name, headers=None):
        self.name = name
        self.headers = headers or []
        try:
            self.parsed_headers = dict(header.split(': ', 1) for header in self.headers)
        except Exception:
            raise CommandError('Could not parse command headers')

    @property
    def call_id(self):
        return self.parsed_headers.get('call_id')


class ControlProtocol(LineOnlyReceiver):
    noisy = False

    def __init__(self):
        self.in_progress = 0

    def lineReceived(self, line):
        raise NotImplementedError()

    def connectionLost(self, reason=connectionDone):
        if isinstance(reason.value, connectionDone.type):
            log.debug('Connection to {} closed'.format(self.description))
        else:
            log.debug('Connection to {} lost: {}'.format(self.description, reason.value))
        self.factory.connection_lost(self)

    def reply(self, reply):
        self.transport.write(reply + self.delimiter)

    def _error_handler(self, failure):
        failure.trap(CommandError, RelayError)
        log.error(failure.value)
        self.reply('error')

    def _catch_all(self, failure):
        log.error(failure.getTraceback())
        self.reply('error')

    def _decrement(self, result):
        self.in_progress = 0
        if self.factory.shutting_down:
            self.transport.loseConnection()

    def _add_callbacks(self, defer):
        defer.addCallback(self.reply)
        defer.addErrback(self._error_handler)
        defer.addErrback(self._catch_all)
        defer.addBoth(self._decrement)


class OpenSIPSControlProtocol(ControlProtocol):
    description = 'OpenSIPS'

    def __init__(self):
        self.request_lines = []
        ControlProtocol.__init__(self)

    def lineReceived(self, line):
        if line == '':
            if self.request_lines:
                self.in_progress += 1
                defer = maybeDeferred(self.handle_request, self.request_lines)
                self._add_callbacks(defer)
                self.request_lines = []
        elif not line.endswith(': '):
            self.request_lines.append(line)

    def handle_request(self, request_lines):
        command = Command(name=request_lines[0], headers=request_lines[1:])
        if command.call_id is None:
            raise CommandError('Request from OpenSIPS is missing the call_id header')
        return self.factory.dispatcher.send_command(command)


class ManagementControlProtocol(ControlProtocol):
    description = 'Management Interface'

    def connectionMade(self):
        if DispatcherConfig.management_use_tls and DispatcherConfig.management_passport is not None:
            peer_cert = self.transport.getPeerCertificate()
            if not DispatcherConfig.management_passport.accept(peer_cert):
                self.transport.loseConnection(CertificateSecurityError('peer certificate not accepted'))
                return

    def lineReceived(self, line):
        if line in ['quit', 'exit']:
            self.transport.loseConnection()
        elif line == 'summary':
            defer = self.factory.dispatcher.relay_factory.get_summary()
            self._add_callbacks(defer)
        elif line == 'sessions':
            defer = self.factory.dispatcher.relay_factory.get_statistics()
            self._add_callbacks(defer)
        elif line == 'version':
            self.reply(__version__)
        else:
            log.error('Unknown command on management interface: %s' % line)
            self.reply('error')


class ControlFactory(Factory):
    noisy = False

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.protocols = []
        self.shutting_down = False

    def buildProtocol(self, addr):
        protocol = Factory.buildProtocol(self, addr)
        self.protocols.append(protocol)
        return protocol

    def connection_lost(self, prot):
        self.protocols.remove(prot)
        if self.shutting_down and len(self.protocols) == 0:
            self.defer.callback(None)

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        if len(self.protocols) == 0:
            return succeed(None)
        else:
            for prot in self.protocols:
                if prot.in_progress == 0:
                    prot.transport.loseConnection()
            self.defer = Deferred()
            return self.defer


class OpenSIPSControlFactory(ControlFactory):
    protocol = OpenSIPSControlProtocol


class ManagementControlFactory(ControlFactory):
    protocol = ManagementControlProtocol


class RelayError(Exception):
    pass


class ConnectionReplaced(ConnectionDone):
    pass


class RelayServerProtocol(LineOnlyReceiver):
    noisy = False
    MAX_LENGTH = 4096*1024 ## (4MB)

    def __init__(self):
        self.commands = {}
        self.halting = False
        self.timedout = False
        self.disconnect_timer = None
        self.sequence_number = 0
        self.authenticated = False

    @property
    def active(self):
        return not self.halting and not self.timedout

    def send_command(self, command):
        log.debug('Issuing %r command to relay at %s' % (command.name, self.ip))
        sequence_number = str(self.sequence_number)
        self.sequence_number += 1
        defer = Deferred()
        timer = reactor.callLater(DispatcherConfig.relay_timeout, self._timeout, sequence_number)
        self.commands[sequence_number] = (command, defer, timer)
        self.transport.write(self.delimiter.join(['{} {}'.format(command.name, sequence_number)] + command.headers) + 2*self.delimiter)
        return defer

    def reply(self, reply):
        self.transport.write(reply + self.delimiter)

    def _timeout(self, sequence_number):
        command, defer, timer = self.commands.pop(sequence_number)
        defer.errback(RelayError('%r command failed: relay at %s timed out' % (command.name, self.ip)))
        if self.timedout is False:
            self.timedout = True
            self.disconnect_timer = reactor.callLater(DispatcherConfig.relay_recover_interval, self.transport.connectionLost, failure.Failure(TCPTimedOutError()))

    def connectionMade(self):
        if DispatcherConfig.passport is not None:
            peer_cert = self.transport.getPeerCertificate()
            if not DispatcherConfig.passport.accept(peer_cert):
                self.transport.loseConnection(CertificateSecurityError('peer certificate not accepted'))
                return
        self.authenticated = True
        self.factory.new_relay(self)

    def lineReceived(self, line):
        try:
            first, rest = line.split(' ', 1)
        except ValueError:
            first = line
            rest = ''
        if first == 'expired':
            try:
                stats = cjson.decode(rest)
            except cjson.DecodeError:
                log.error('Error decoding JSON from relay at %s' % self.ip)
            else:
                call_id = stats['call_id']
                session = self.factory.sessions.get(call_id, None)
                if session is None:
                    log.error('Unknown session with call_id %s expired at relay %s' % (call_id, self.ip))
                    return
                if session.relay_ip != self.ip:
                    log.error('session with call_id %s expired at relay %s, but is actually at relay %s, ignoring' % (call_id, self.ip, session.relay_ip))
                    return
                all_streams_ice = all(stream_info['status'] == 'unselected ICE candidate' for stream_info in stats['streams'])
                if all_streams_ice:
                    log.info('session with call_id %s from relay %s removed because ICE was used' % (call_id, session.relay_ip))
                    stats['timed_out'] = False
                else:
                    log.info('session with call_id %s from relay %s did timeout' % (call_id, session.relay_ip))
                    stats['timed_out'] = True
                stats['dialog_id'] = session.dialog_id
                stats['all_streams_ice'] = all_streams_ice
                self.factory.dispatcher.update_statistics(stats)
                if session.dialog_id is not None and stats['start_time'] is not None and not all_streams_ice:
                    self.factory.dispatcher.opensips_management.end_dialog(session.dialog_id)
                    session.expire_time = time()
                else:
                    del self.factory.sessions[call_id]
            return
        elif first == 'ping':
            if self.timedout is True:
                self.timedout = False
                if self.disconnect_timer.active():
                    self.disconnect_timer.cancel()
                self.disconnect_timer = None
            self.reply('pong')
            return
        try:
            command, defer, timer = self.commands.pop(first)
        except KeyError:
            log.error('Got unexpected response from relay at %s: %s' % (self.ip, line))
            return
        timer.cancel()
        if rest == 'error':
            defer.errback(RelayError('Received error from relay at %s in response to %r command' % (self.ip, command.name)))
        elif rest == 'halting':
            self.halting = True
            defer.errback(RelayError('Relay at %s is shutting down' % self.ip))
        elif command.name == 'remove':
            try:
                stats = cjson.decode(rest)
            except cjson.DecodeError:
                log.error('Error decoding JSON from relay at %s' % self.ip)
            else:
                call_id = stats['call_id']
                session = self.factory.sessions[call_id]
                stats['dialog_id'] = session.dialog_id
                stats['timed_out'] = False
                self.factory.dispatcher.update_statistics(stats)
                del self.factory.sessions[call_id]
            defer.callback('removed')
        else:  # update command
            defer.callback(rest)

    def connectionLost(self, reason=connectionDone):
        if reason.type == ConnectionDone:
            log.info('Connection with relay at %s was closed' % self.ip)
        elif reason.type == ConnectionReplaced:
            log.warning('Old connection with relay at %s was lost' % self.ip)
        else:
            log.error('Connection with relay at %s was lost: %s' % (self.ip, reason.value))
        for command, defer, timer in self.commands.itervalues():
            timer.cancel()
            defer.errback(RelayError('%r command failed: relay at %s disconnected' % (command.name, self.ip)))
        if self.timedout is True:
            self.timedout = False
            if self.disconnect_timer.active():
                self.disconnect_timer.cancel()
            self.disconnect_timer = None
        self.factory.connection_lost(self)


class RelaySession(object):
    def __init__(self, relay_ip, command):
        self.relay_ip = relay_ip
        self.dialog_id = command.parsed_headers.get('dialog_id')
        self.expire_time = None


class RelayFactory(Factory):
    noisy = False
    protocol = RelayServerProtocol

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.relays = {}
        self.shutting_down = False
        state_file = process.runtime.file('dispatcher_state')
        try:
            self.sessions = pickle.load(open(state_file))
        except:
            self.sessions = {}
            self.cleanup_timers = {}
        else:
            self.cleanup_timers = dict((ip, reactor.callLater(DispatcherConfig.cleanup_dead_relays_after, self._do_cleanup, ip)) for ip in set(session.relay_ip for session in self.sessions.itervalues()))
        unlink(state_file)
        self.expired_cleaner = RecurrentCall(600, self._remove_expired_sessions)

    def _remove_expired_sessions(self):
        now, limit = time(), DispatcherConfig.cleanup_expired_sessions_after
        obsolete = [k for k, s in ifilter(lambda (k, s): s.expire_time and (now-s.expire_time>=limit), self.sessions.iteritems())]
        if obsolete:
            [self.sessions.pop(call_id) for call_id in obsolete]
            log.warning('found %d expired sessions which were not removed during the last %d hours' % (len(obsolete), round(limit / 3600.0)))
        return KeepRunning

    def buildProtocol(self, addr):
        ip = addr.host
        log.debug('Connection from relay at %s' % ip)
        protocol = Factory.buildProtocol(self, addr)
        protocol.ip = ip
        return protocol

    def new_relay(self, relay):
        old_relay = self.relays.pop(relay.ip, None)
        if old_relay is not None:
            log.warning('Relay at %s reconnected, closing old connection' % relay.ip)
            reactor.callLater(0, old_relay.transport.connectionLost, failure.Failure(ConnectionReplaced("relay reconnected")))
        self.relays[relay.ip] = relay
        timer = self.cleanup_timers.pop(relay.ip, None)
        if timer is not None:
            timer.cancel()
        defer = relay.send_command(Command('sessions'))
        defer.addCallback(self._cb_purge_sessions, relay.ip)

    def _cb_purge_sessions(self, result, relay_ip):
        relay_sessions = cjson.decode(result)
        relay_call_ids = [session['call_id'] for session in relay_sessions]
        for session_id, session in self.sessions.items():
            if session.expire_time is None and session.relay_ip == relay_ip and session_id not in relay_call_ids:
                log.warning('Session %s is no longer on relay %s, statistics are probably lost' % (session_id, relay_ip))
                if session.dialog_id is not None:
                    self.dispatcher.opensips_management.end_dialog(session.dialog_id)
                del self.sessions[session_id]

    def send_command(self, command):
        session = self.sessions.get(command.call_id, None)
        if session and session.expire_time is None:
            relay = session.relay_ip
            if relay not in self.relays:
                raise RelayError('Relay for this session (%s) is no longer connected' % relay)
            return self.relays[relay].send_command(command)
        # We do not have a session for this call_id or the session is already expired
        if command.name == 'update':
            preferred_relay = command.parsed_headers.get('media_relay')
            try_relays = deque(protocol for protocol in self.relays.itervalues() if protocol.active and protocol.ip != preferred_relay)
            random.shuffle(try_relays)
            if preferred_relay is not None:
                protocol = self.relays.get(preferred_relay)
                if protocol is not None and protocol.active:
                    try_relays.appendleft(protocol)
                else:
                    log.warning('user requested media_relay %s is not available' % preferred_relay)
            defer = self._try_next(try_relays, command)
            defer.addCallback(self._add_session, try_relays, command)
            return defer
        elif command.name == 'remove' and session:
            # This is the remove we received for an expired session for which we triggered dialog termination
            del self.sessions[command.call_id]
            return 'removed'
        else:
            raise RelayError('Got {0.name!r} command from OpenSIPS for unknown session with call-id {0.call_id}'.format(command))

    def _add_session(self, result, try_relays, command):
        self.sessions[command.call_id] = RelaySession(try_relays[0].ip, command)
        return result

    def _relay_error(self, failure, try_relays, command):
        failure.trap(RelayError)
        failed_relay = try_relays.popleft()
        log.warning('Relay %s failed: %s' % (failed_relay, failure.value))
        return self._try_next(try_relays, command)

    def _try_next(self, try_relays, command):
        if len(try_relays) == 0:
            raise RelayError('No suitable relay found')
        defer = try_relays[0].send_command(command)
        defer.addErrback(self._relay_error, try_relays, command)
        return defer

    def get_summary(self):
        defer = DeferredList([relay.send_command(Command('summary')).addErrback(self._summary_error, ip) for ip, relay in self.relays.iteritems()])
        defer.addCallback(self._got_summaries)
        return defer

    def _summary_error(self, failure, ip):
        log.error('Error processing query at relay %s: %s' % (ip, failure.value))
        return cjson.encode(dict(status="error", ip=ip))

    def _got_summaries(self, results):
        return '[%s]' % ', '.join(result for succeeded, result in results if succeeded)

    def get_statistics(self):
        defer = DeferredList([relay.send_command(Command('sessions')) for relay in self.relays.itervalues()])
        defer.addCallback(self._got_statistics)
        return defer

    def _got_statistics(self, results):
        return '[%s]' % ', '.join(result[1:-1] for succeeded, result in results if succeeded and result != '[]')

    def connection_lost(self, relay):
        if relay not in self.relays.itervalues():
            return
        if relay.authenticated:
            del self.relays[relay.ip]
        if self.shutting_down:
            if len(self.relays) == 0:
                self.defer.callback(None)
        else:
            self.cleanup_timers[relay.ip] = reactor.callLater(DispatcherConfig.cleanup_dead_relays_after, self._do_cleanup, relay.ip)

    def _do_cleanup(self, ip):
        log.debug('Doing cleanup for old relay %s' % ip)
        del self.cleanup_timers[ip]
        for call_id in [call_id for call_id, session in self.sessions.items() if session.relay_ip == ip]:
            del self.sessions[call_id]

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        for timer in self.cleanup_timers.itervalues():
            timer.cancel()
        if len(self.relays) == 0:
            retval = succeed(None)
        else:
            for prot in self.relays.itervalues():
                prot.transport.loseConnection()
            self.defer = Deferred()
            retval = self.defer
        retval.addCallback(self._save_state)
        return retval

    def _save_state(self, result):
        pickle.dump(self.sessions, open(process.runtime.file('dispatcher_state'), 'w'))


class Dispatcher(object):

    def __init__(self):
        self.accounting = [__import__('mediaproxy.interfaces.accounting.%s' % mod.lower(), globals(), locals(), ['']).Accounting() for mod in set(DispatcherConfig.accounting)]
        self.cred = X509Credentials(cert_name='dispatcher')
        self.tls_context = TLSContext(self.cred)
        self.relay_factory = RelayFactory(self)
        dispatcher_addr, dispatcher_port = DispatcherConfig.listen
        self.relay_listener = reactor.listenTLS(dispatcher_port, self.relay_factory, self.tls_context, interface=dispatcher_addr)
        self.opensips_factory = OpenSIPSControlFactory(self)
        socket_path = process.runtime.file(DispatcherConfig.socket_path)
        unlink(socket_path)
        self.opensips_listener = reactor.listenUNIX(socket_path, self.opensips_factory)
        self.opensips_management = opensips.ManagementInterface()
        self.management_factory = ManagementControlFactory(self)
        management_addr, management_port = DispatcherConfig.listen_management
        if DispatcherConfig.management_use_tls:
            self.management_listener = reactor.listenTLS(management_port, self.management_factory, self.tls_context, interface=management_addr)
        else:
            self.management_listener = reactor.listenTCP(management_port, self.management_factory, interface=management_addr)

    def run(self):
        log.debug('Using {0.__class__.__name__}'.format(reactor))
        process.signals.add_handler(signal.SIGHUP, self._handle_SIGHUP)
        process.signals.add_handler(signal.SIGINT, self._handle_SIGINT)
        process.signals.add_handler(signal.SIGTERM, self._handle_SIGTERM)
        for accounting_module in self.accounting:
            accounting_module.start()
        reactor.run(installSignalHandlers=False)

    def send_command(self, command):
        return maybeDeferred(self.relay_factory.send_command, command)

    def update_statistics(self, stats):
        log.debug('Got statistics: %s' % stats)
        if stats['start_time'] is not None:
            for accounting in self.accounting:
                try:
                    accounting.do_accounting(stats)
                except Exception, e:
                    log.exception('An unhandled error occurred while doing accounting: %s' % e)

    def _handle_SIGHUP(self, *args):
        log.info('Received SIGHUP, shutting down.')
        reactor.callFromThread(self._shutdown)

    def _handle_SIGINT(self, *args):
        if process.daemon:
            log.info('Received SIGINT, shutting down.')
        else:
            log.info('Received KeyboardInterrupt, exiting.')
        reactor.callFromThread(self._shutdown)

    def _handle_SIGTERM(self, *args):
        log.info('Received SIGTERM, shutting down.')
        reactor.callFromThread(self._shutdown)

    def _shutdown(self):
        defer = DeferredList([result for result in [self.opensips_listener.stopListening(), self.management_listener.stopListening(), self.relay_listener.stopListening()] if result is not None])
        defer.addCallback(lambda x: self.opensips_factory.shutdown())
        defer.addCallback(lambda x: self.management_factory.shutdown())
        defer.addCallback(lambda x: self.relay_factory.shutdown())
        defer.addCallback(lambda x: self._stop())

    def _stop(self):
        for act in self.accounting:
            act.stop()
        reactor.stop()
