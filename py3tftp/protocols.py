import asyncio
import logging

from py3tftp import file_io
from py3tftp import tftp_parsing
from py3tftp.exceptions import ProtocolException
from py3tftp.tftp_packet import TFTPPacketFactory


logger = logging.getLogger(__name__)


class BaseTFTPProtocol(asyncio.DatagramProtocol):
    supported_opts = {
        b'blksize': tftp_parsing.blksize_parser,
        b'timeout': tftp_parsing.timeout_parser,
        b'tsize': tftp_parsing.tsize_parser,
        b'windowsize': tftp_parsing.windowsize_parser,
    }

    default_opts = {b'timeout': 0.5, b'conn_timeout': 5.0, b'blksize': 512,
                    b'windowsize': 1}

    def __init__(self, packet, file_handler_cls, remote_addr, extra_opts=None):
        self.packet_factory = TFTPPacketFactory(
            supported_opts=self.supported_opts,
            default_opts=self.default_opts)

        self.remote_addr = remote_addr
        self.packet = self.packet_factory.from_bytes(packet)
        self.extra_opts = extra_opts or {}
        self.file_handler_cls = file_handler_cls
        self.retransmit = None
        self.file_handler = None
        self.finished = False
        self.retransmits = []

    def datagram_received(self, data, addr):
        """
        Processes every received datagram.
        """
        raise NotImplementedError

    def initialize_transfer(self):
        """
        Sets up the message counter and attempts to open the target file for
        reading or writing.
        """
        raise NotImplementedError

    def next_datagram(self):
        """
        Returns the next datagram to be sent to self.remote_addr.
        """
        raise NotImplementedError

    def connection_made(self, transport):
        """
        Triggers connection initialization at the beginning of a connection.
        """
        self.transport = transport
        self.handle_initialization()

    def handle_initialization(self):
        """
        Sends first packet to self.remote_addr. In the process, it attempts to
        access the requested file - and handles possible file errors - as well
        as handling option negotiation (if applicable).
        """
        logger.debug('Initializing file transfer to {addr}'.format(
            addr=self.remote_addr))
        try:
            self.set_proto_attributes()
            self.initialize_transfer()

            if self.r_opts:
                self.counter = 0
                pkt = self.packet_factory.create_packet('OCK',
                                                        r_opts=self.r_opts)
            else:
                pkt = self.next_datagram()
        except FileExistsError:
            logger.error('"{}" already exists! Cannot overwrite'.format(
                self.filename))
            pkt = self.packet_factory.err_file_exists()
        except PermissionError:
            logger.error('Insufficient permissions to operate on "{}"'.format(
                self.filename))
            pkt = self.packet_factory.err_access_violation()
        except FileNotFoundError:
            logger.error('File "{}" does not exist!'.format(self.filename))
            pkt = self.packet_factory.err_file_not_found()

        logger.debug('opening pkt: {}'.format(pkt))
        self.send_opening_packet(pkt.to_bytes())

        if pkt.is_err():
            self.handle_err_pkt()

    def set_proto_attributes(self):
        """
        Sets the self.filename , self.opts, and self.r_opts.
        The caller should handle any exceptions and react accordingly
        ie. send error packet, close connection, etc.
        """
        self.filename = self.packet.fname
        self.r_opts = self.packet.r_opts
        self.opts = {**self.default_opts, **self.extra_opts, **self.r_opts}
        logger.debug(
            'Set protocol attributes as {attrs}'.format(attrs=self.opts))

    def connection_lost(self, exc):
        """
        Cleans up socket and fd after connection has been lost. Logs an error
        if connection interrupted.
        """
        self.conn_reset()
        if exc:
            logger.error(
                'Error on connection lost: {0}.\nTraceback: {1}'.format(
                    exc, exc.__traceback__))
        else:
            logger.info('Connection to {0}:{1} terminated'.format(
                *self.remote_addr))

    def error_received(self, exc):
        """
        Handles cleanup after socket reports an error ie. local or remote
        socket closed and other network errors.
        """

        self.conn_reset()
        self.transport.close()
        logger.error(
            ('Error receiving packet from {0}: {1}. '
             'Transfer of "{2}" aborted.\nTraceback: {3}').format(
                 self.remote_addr, exc, self.filename, exc.__traceback__))

    def send_opening_packet(self, packet):
        """
        Starts the connection timeout timer and sends first datagram.
        """
        self.reply_to_client(packet)
        self.h_timeout = asyncio.get_event_loop().call_later(
            self.opts[b'conn_timeout'], self.conn_timeout)

    def reply_to_client(self, packet):
        """
        Starts the message retry loop, resending packet to self.remote_addr
        every 'timeout'.
        """
        self.transport.sendto(packet, self.remote_addr)
        self.retransmit = asyncio.get_event_loop().call_later(
            self.opts[b'timeout'], self.reply_to_client, packet)
        if self.opts[b'windowsize'] > 1:
            self.retransmits.append(self.retransmit)

    def handle_err_pkt(self):
        """
        Cleans up connection after sending a courtesy error packet
        to offending client.
        """
        logger.info(('Closing connection to {0} due to error. '
                     '"{1}" Not transmitted.').format(self.remote_addr,
                                                      self.filename))
        self.conn_reset()
        asyncio.get_event_loop().call_soon(self.transport.close)

    def retransmit_reset(self):
        """
        Stops the message retry loop.
        """
        if self.opts[b'windowsize'] > 1:
            [retransmit.cancel() for retransmit in self.retransmits]
            self.retransmits = []
        else:
            if self.retransmit:
                self.retransmit.cancel()

    def conn_reset(self):
        """
        Stops the message retry loop and the connection timeout timers.
        """
        self.retransmit_reset()
        if self.h_timeout:
            self.h_timeout.cancel()

    def conn_timeout(self):
        """
        Cleans up timers and the connection when called.
        """

        logger.error(
            'Connection to {0} timed out, "{1}" not transfered'.format(
                self.remote_addr, self.filename))
        self.retransmit_reset()
        self.transport.close()

    def conn_timeout_reset(self):
        """
        Restarts the connection timeout timer.
        """

        self.conn_reset()
        self.h_timeout = asyncio.get_event_loop().call_later(
            self.opts[b'conn_timeout'], self.conn_timeout)

    def is_correct_tid(self, addr):
        """
        Checks whether address '(ip, port)' matches that of the
        established remote host.
        May send error to host that submitted incorrect address.
        """
        if self.remote_addr[1] == addr[1]:
            return True
        else:
            logger.info(
                'Unknown transfer id: expected {0}, got {1} instead.'.format(
                    self.remote_addr, addr))
            err_response = self.packet_factory.err_unknown_tid()
            self.transport.sendto(err_response.to_bytes(), addr)
            return False


class WRQProtocol(BaseTFTPProtocol):
    def __init__(self, wrq, file_handler_cls, addr, opts):
        super().__init__(wrq, file_handler_cls, addr, opts)
        logger.info('Initiating WRQProtocol with {0}'.format(
            self.remote_addr))

    def next_datagram(self):
        """
        Builds an acknowledgement of a received data packet.
        """
        return self.packet_factory.create_packet(pkt_type='ACK',
                                                 block_no=self.counter)

    def initialize_transfer(self):
        self.counter = 0
        self.file_handler = self.file_handler_cls(self.filename,
                                                  self.opts[b'blksize'])

    def datagram_received(self, data, addr):
        """
        Check correctness of received datagram, reset timers, increment
        counter, ACKnowledge datagram, save received data to file.
        """
        packet = self.packet_factory.from_bytes(data)

        if (self.is_correct_tid(addr) and packet.is_data() and
                packet.is_correct_sequence((self.counter + 1) % 65536)):
            self.conn_timeout_reset()

            self.counter = (self.counter + 1) % 65536
            reply_packet = self.next_datagram()
            self.reply_to_client(reply_packet.to_bytes())

            self.file_handler.write_chunk(packet.data)

            if packet.size < self.opts[b'blksize']:
                logger.info('Receiving file "{0}" from {1} completed'.format(
                    self.filename, self.remote_addr))
                self.retransmit_reset()
                self.transport.close()
        else:
            logger.debug('Data: {0}; is_data: {1}; counter: {2}'.format(
                data, packet.is_data(), self.counter))


class RRQProtocol(BaseTFTPProtocol):
    def __init__(self, rrq, file_handler_cls, addr, opts):
        super().__init__(rrq, file_handler_cls, addr, opts)
        logger.info('Initiating RRQProtocol with {0}'.format(
            self.remote_addr))

    def next_datagram(self):
        return self.packet_factory.create_packet(
            pkt_type='DAT',
            block_no=self.counter,
            data=self.file_handler.read_chunk())

    def initialize_transfer(self):
        self.counter = 1
        self.file_handler = self.file_handler_cls(self.filename,
                                                  self.opts[b'blksize'])
        if b'tsize' in self.r_opts:
            self.r_opts[b'tsize'] = self.file_handler.file_size()
        if self.opts[b'windowsize'] > 1:
            self.packets = [None] * self.opts[b'windowsize']

    def datagram_received_default(self, data, addr):
        """
        Checks correctness of incoming datagrams, reset timers,
        increments message counter, send next chunk of requested file
        to client. Works only for windowsize=1 (default value)
        """
        packet = self.packet_factory.from_bytes(data)
        if (self.is_correct_tid(addr) and packet.is_err()):
            self.handle_err_pkt()
            return
        if (self.is_correct_tid(addr) and packet.is_ack() and
                packet.is_correct_sequence(self.counter)):
            self.conn_timeout_reset()
            if self.file_handler.finished:
                self.transport.close()
                return
            self.counter = (self.counter + 1) % 65536
            packet = self.next_datagram()
            self.reply_to_client(packet.to_bytes())
        else:
            logger.debug('Ack: {0}; is_ack: {1}; counter: {2}'.format(
                data, packet.is_ack(), self.counter))

    def is_packet_inside_window(self, packet, windowsize):
        return ((packet.block_no > (self.counter - windowsize)) and (
            packet.block_no <= self.counter))

    def datagram_received_windowsize(self, data, addr, windowsize):
        """
        Checks correctness of incoming datagrams, reset timers,
        increments message counter, send next chunk of requested file
        to client, and according to the agreed windowsize.
        """
        packet = self.packet_factory.from_bytes(data)
        if (self.is_correct_tid(addr) and packet.is_err()):
            self.handle_err_pkt()
            return
        if (self.is_correct_tid(addr) and packet.is_ack() and
                self.is_packet_inside_window(packet, windowsize)):
            self.conn_timeout_reset()
            if packet.is_correct_sequence(self.counter):
                if self.file_handler.finished:  # ACK of last package arrived
                    self.transport.close()
                    return
                newpknum = windowsize  # start next window
            else:  # faulty ACK in current window transmission
                # TODO: this might not work if the counter rolled over
                cntdif = self.counter - packet.block_no
                newpknum = windowsize - cntdif
                self.counter += 1 - newpknum
            for i in range(newpknum):
                if self.file_handler.finished:  # last window
                    # discard excess packets (older are first in the list)
                    self.packets = self.packets[-i:]
                    break
                # self.packets always contains at most windowsize items
                self.packets.pop(0)  # discard old packets
                self.counter = (self.counter + 1) % 65536
                packet = self.next_datagram()
                self.packets.append(packet)
            for packet in self.packets:
                self.reply_to_client(packet.to_bytes())
        else:
            logger.debug('Ack: {0}; is_ack: {1}; counter: {2}'.format(
                data, packet.is_ack(), self.counter))

    def datagram_received(self, data, addr):
        """
        Checks correctness of incoming datagrams, reset timers,
        increments message counter, send next chunk of requested file
        to client.
        """
        if self.opts[b'windowsize'] > 1:
            self.datagram_received_windowsize(data, addr,
                                              self.opts[b'windowsize'])
        else:
            self.datagram_received_default(data, addr)


class BaseTFTPServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, host_interface, loop, extra_opts):
        self.host_interface = host_interface
        self.loop = loop
        self.extra_opts = extra_opts
        self.packet_factory = TFTPPacketFactory()

    def select_protocol(self, request):
        """
        Selects an asyncio.Protocol-compatible protocol to
        feed to an event loop's 'create_datagram_endpoint'
        function.
        """
        raise NotImplementedError

    def select_file_handler(self, first_packet):
        """
        Selects a class that implements the correct interface
        to handle the input/output for a tftp transfer.
        """
        raise NotImplementedError

    def connection_made(self, transport):
        logger.info('Listening...')
        self.transport = transport

    def datagram_received(self, data, addr):
        """
        Opens a read or write connection to remote host by scheduling
        an asyncio.Protocol.
        """
        logger.debug('received: {}'.format(data.decode()))

        first_packet = self.packet_factory.from_bytes(data)
        protocol = self.select_protocol(first_packet)
        file_handler_cls = self.select_file_handler(first_packet)

        connect = self.loop.create_datagram_endpoint(
            lambda: protocol(data, file_handler_cls, addr, self.extra_opts),
            sock=self.transport.get_extra_info('socket'))

        self.loop.create_task(connect)

    def connection_lost(self, exc):
        logger.info('TFTP server - connection lost')


class TFTPServerProtocol(BaseTFTPServerProtocol):
    def select_protocol(self, packet):
        logger.debug('packet type: {}'.format(packet.pkt_type))
        if packet.is_rrq():
            return RRQProtocol
        elif packet.is_wrq():
            return WRQProtocol
        else:
            raise ProtocolException('Received incompatible request, ignoring.')

    def select_file_handler(self, packet):
        if packet.is_wrq():
            return lambda filename, opts: file_io.FileWriter(
                filename, opts, packet.mode)
        else:
            return lambda filename, opts: file_io.FileReader(
                filename, opts, packet.mode)
