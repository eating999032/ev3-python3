#!/usr/bin/env python3
'''
LEGO EV3 direct commands - ev3
'''
# Adding support for win10 methods
# Make sure you have pybluez installed on Win10 - see http://ev3directcommands.blogspot.com/2016/01/no-title-specified-page-table-border_94.html

import re
import bluetooth
import usb.util
import socket
import struct
from time import sleep
from datetime import datetime
from threading import Lock
from numbers import Integral
from .exceptions import DirCmdError, SysCmdError
from .constants import (
    _ID_VENDOR_LEGO,
    _ID_PRODUCT_EV3,
    _EP_IN,
    _EP_OUT,
    _DIRECT_COMMAND_REPLY,
    _DIRECT_COMMAND_NO_REPLY,
    _DIRECT_REPLY,
    _SYSTEM_COMMAND_REPLY,
    _SYSTEM_COMMAND_NO_REPLY,
    _SYSTEM_REPLY,
    WIFI,
    BLUETOOTH,
    USB,
    STD,
    ASYNC,
    SYNC
)


class PhysicalEV3:
    '''
    holds data and methods, which are singletons per physical EV3 device
    '''
    def __init__(
        self,
        protocol: str,
        host: str,
    ):
        assert protocol in (BLUETOOTH, WIFI, USB), \
            'Protocol ' + protocol + 'is not valid'
        assert isinstance(host, str), \
            "host needs to be of type str"

        self._msg_cnt = 41
        self._lock = Lock()
        self._reply_buffer = {}
        self._protocol = protocol
        self._host = host
        self._device = None
        self._socket = None

        if protocol == BLUETOOTH:
            self._socket = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            self._socket.connect((host, 1))

            # self._connect_bluetooth()
        elif protocol == WIFI:
            self._connect_wifi()
        else:
            self._connect_usb()

    def __del__(self):
        """
        closes the connection to the LEGO EV3
        """
        if isinstance(self._socket, bluetooth.BluetoothSocket):
            self._socket.close()
        # if (
        #     self._socket is not None and
        #     isinstance(self._socket, socket.socket)
        # ):
        #     self._socket.close()

    def next_msg_cnt(self) -> int:
        '''
        determines next message counter
        '''
        if self._msg_cnt < 32767:
            self._msg_cnt += 1
        else:
            self._msg_cnt = 1
        return self._msg_cnt

    def put_to_reply_buffer(self, msg_cnt: bytes, reply: bytes) -> None:
        """
        put a reply to the stack
        """
        if msg_cnt in self._reply_buffer:
            raise ValueError(
                'reply with msg_cnt ' +
                ':'.join('{:02X}'.format(byte) for byte in msg_cnt) +
                ' already exists'
            )
        else:
            self._reply_buffer[msg_cnt] = reply

    def _connect_bluetooth(self) -> int:
        """
        Create a socket, that holds a bluetooth-connection to an EV3
        """
        self._socket = socket.socket(
            socket.AF_BLUETOOTH,
            socket.SOCK_STREAM,
            socket.BTPROTO_RFCOMM
        )
        self._socket.connect((self._host, 1))

    def _connect_wifi(self) -> int:
        """
        Create a socket, that holds a wifi-connection to an EV3
        """

        # listen on port 3015 for a UDP broadcast from the EV3
        UDPSock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )
        UDPSock.bind(('', 3015))
        data, addr = UDPSock.recvfrom(67)

        # pick serial number, port, name and protocol
        # from the broadcast message
        matcher = re.search(
            r'^Serial-Number: (\w*)\s\n' +
            r'Port: (\d{4,4})\s\n' +
            r'Name: (\w+)\s\n' +
            r'Protocol: (\w+)$',
            data.decode('utf-8')
        )
        serial_number = matcher.group(1)
        port = matcher.group(2)
        name = matcher.group(3)
        protocol = matcher.group(4)

        # test if correct mac-addr
        if (
                self._host and
                serial_number.upper() != self._host.replace(':', '').upper()
        ):
            self._socket = None
            raise ValueError('found ev3 but not ' + self._host)

        # Send an UDP message back to the EV3
        # to make it accept a TCP/IP connection
        UDPSock.sendto(' '.encode('utf-8'), (addr[0], int(port)))
        UDPSock.close()

        # Establish a TCP/IP connection with EV3's address and port
        self._socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )
        self._socket.connect((addr[0], int(port)))

        # Send an unlock message to the EV3 over TCP/IP
        msg = 'GET /target?sn=' + serial_number + 'VMTP1.0\n' + \
              'Protocol: ' + protocol
        self._socket.send(msg.encode('utf-8'))
        reply = self._socket.recv(16).decode('utf-8')
        if not reply.startswith('Accept:EV340'):
            raise RuntimeError(
                'No wifi connection to ' +
                name +
                ' established'
            )

    def _connect_usb(self) -> int:
        """
        Create a device, that holds an usb-connection to an EV3
        """
        ev3_devices = usb.core.find(
            find_all=True,
            idVendor=_ID_VENDOR_LEGO,
            idProduct=_ID_PRODUCT_EV3
        )
        for dev in ev3_devices:
            if self._device:
                raise ValueError(
                    'found multiple ev3 but no argument host was set'
                )
            if self._host:
                mac_addr = usb.util.get_string(dev, dev.iSerialNumber)
                if mac_addr.upper() == self._host.replace(':', '').upper():
                    self._device = dev
                    break
            else:
                self._device = dev
        if not self._device:
            raise RuntimeError("Lego EV3 not found")
        if self._device.is_kernel_driver_active(0) is True:
            self._device.detach_kernel_driver(0)
        self._device.set_configuration()

        # initial read
        self._device.read(_EP_IN, 1024, 100)


class EV3:
    """
    communicates with a LEGO EV3 using direct commands
    """

    def __init__(
        self,
        protocol: str = None,
        host: str = None,
        ev3_obj: 'EV3' = None,
        sync_mode: str = None,
        verbosity=0
    ):
        """
        Establish a connection to a LEGO EV3 device

        Keyword Arguments (either protocol and host or ev3_obj):
          protocol
            'Bluetooth', 'Usb' or 'Wifi'
          host
            MAC-address of the LEGO EV3 (f.i. '00:16:53:42:2B:99')
          ev3_obj
            existing EV3 object (its connections will be used)
          sync mode (standard, asynchronous, synchronous)
            STD
              Use DIRECT_COMMAND_REPLY if global_mem > 0,
              then wait for reply.
            ASYNC
              Use DIRECT_COMMAND_REPLY if global_mem > 0,
              never wait for reply (it's the task of the calling program).
            SYNC
              Always use DIRECT_COMMAND_REPLY and wait for reply.
          verbosity
            level (0, 1, 2) of verbosity (prints on stdout).
        """
        assert ev3_obj or protocol, \
            'Either protocol or ev3_obj needs to be given'
        if ev3_obj:
            assert isinstance(ev3_obj, EV3), \
                'ev3_obj needs to be instance of EV3'
            self._physical_ev3 = ev3_obj._physical_ev3
        else:
            self._physical_ev3 = PhysicalEV3(protocol, host)

        assert isinstance(verbosity, Integral), \
            "verbosity needs to be of type int"
        assert verbosity >= 0 and verbosity <= 2, \
            "allowed verbosity values are: 0, 1 or 2"
        self._verbosity = int(verbosity)

        assert sync_mode is None or isinstance(sync_mode, str), \
            "sync_mode needs to be of type str"
        assert sync_mode is None or sync_mode in (STD, SYNC, ASYNC), \
            "value of sync_mode: " + sync_mode + " is invalid"

        if sync_mode is None and self._physical_ev3._protocol == USB:
            self._sync_mode = SYNC
        else:
            self._sync_mode = STD

    @property
    def sync_mode(self) -> str:
        """
        sync mode (standard, asynchronous, synchronous)

        STD
          use DIRECT_COMMAND_REPLY if global_mem > 0,
          wait for reply if there is one.
        ASYNC
          use DIRECT_COMMAND_REPLY if global_mem > 0,
          never wait for reply (it's the task of the calling program).
        SYNC
          always use DIRECT_COMMAND_REPLY and wait for reply.

        The idea is:
          ASYNC
            Interruption or EV3 device queues direct commands,
            control directly comes back.
          SYNC
            EV3 device is blocked until direct command is finished,
            control comes back, when direct command is finished.
          STD
            NO_REPLY like ASYNC with interruption or EV3 queuing,

            REPLY like SYNC, synchronicity of program and EV3 device.
        """

        return self._sync_mode

    @sync_mode.setter
    def sync_mode(self, value: str):
        assert isinstance(value, str), \
            "sync_mode needs to be of type str"
        assert value in (STD, SYNC, ASYNC), \
            "value of sync_mode: " + value + " is invalid"
        self._sync_mode = value

    @property
    def verbosity(self) -> int:
        """
        level of verbosity (prints on stdout).
        """
        return self._verbosity

    @verbosity.setter
    def verbosity(self, value: Integral):
        assert isinstance(value, Integral), \
            "verbosity needs to be of type int"
        assert value >= 0 and value <= 2, \
            "allowed verbosity values are: 0, 1 or 2"
        self._verbosity = int(value)

    def send_direct_cmd(
        self,
        ops: bytes,
        local_mem: Integral = 0,
        global_mem: Integral = 0,
        sync_mode: str = None,
        verbosity: Integral = None
    ) -> bytes:
        """
        Send a direct command to the LEGO EV3

        Positional Arguments
          ops
            holds netto data only (operations), these fields are added:
              length: 2 bytes, little endian

              msg_cnt: 2 bytes, little endian

              type: 1 byte, DIRECT_COMMAND_REPLY or DIRECT_COMMAND_NO_REPLY

              header: 2 bytes, holds sizes of local and global memory

        Keyword Arguments
          local_mem
            size of the local memory
          global_mem
            size of the global memory
          sync_mode
            synchronization mode (STD, SYNC, ASYNC)
          verbosity
            level (0, 1, 2) of verbosity (prints on stdout).

        Returns
          if sync_mode is STD
            reply (if global_mem > 0) or message counter
          if sync_mode is ASYNC
            message counter
          if sync_mode is SYNC
            reply of the LEGO EV3
        """
        assert isinstance(ops, bytes), \
            "ops needs to be of type bytes"
        assert len(ops) > 0, \
            "ops must not be empty"
        assert isinstance(local_mem, Integral), \
            "local_mem needs to be an integer"
        assert local_mem >= 0, \
            "local_mem needs to be positive"
        assert local_mem <= 63, \
            "local_mem has a maximum of 63"
        assert isinstance(global_mem, Integral), \
            "global_mem needs to be an integer"
        assert global_mem >= 0, \
            "global_mem needs to be positive"
        assert local_mem <= 1019, \
            "global_mem has a maximum of 1019"
        assert sync_mode is None or isinstance(sync_mode, str), \
            "sync_mode needs to be of type str"
        assert sync_mode is None or sync_mode in (STD, SYNC, ASYNC), \
            "value of sync_mode: " + sync_mode + " is invalid"
        assert verbosity is None or isinstance(verbosity, Integral), \
            "verbosity needs to be of type int"
        assert verbosity is None or verbosity >= 0 and verbosity <= 2, \
            "allowed verbosity values are: 0, 1 or 2"

        self._physical_ev3._lock.acquire()

        if (
                global_mem > 0 or
                sync_mode == SYNC or
                sync_mode is None and self._sync_mode == SYNC
        ):
            cmd_type = _DIRECT_COMMAND_REPLY
        else:
            cmd_type = _DIRECT_COMMAND_NO_REPLY

        msg_cnt = self._physical_ev3.next_msg_cnt()

        cmd = b''.join((
            struct.pack('<hh', len(ops) + 5, msg_cnt),
            cmd_type,
            struct.pack('<h', local_mem * 1024 + global_mem),
            ops
        ))

        if (
                verbosity is not None and verbosity > 0 or
                verbosity is None and self._verbosity > 0
        ):
            print(
                datetime.now().strftime('%H:%M:%S.%f') +
                ' Sent 0x|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[0:2]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[2:4]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[4:5]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[5:7]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[7:]) + '|'
            )

        if self._physical_ev3._protocol in (BLUETOOTH, WIFI):
            self._physical_ev3._socket.send(cmd)
        else:
            self._physical_ev3._device.write(_EP_OUT, cmd, 100)

        msg_cnt = cmd[2:4]
        if (
                cmd[4:5] == _DIRECT_COMMAND_NO_REPLY or
                sync_mode == ASYNC or
                sync_mode is None and self._sync_mode == ASYNC
        ):
            self._physical_ev3._lock.release()
            return msg_cnt
        else:
            return self.wait_for_reply(
                msg_cnt,
                verbosity=verbosity,
                _locked=True
            )

    def wait_for_reply(
        self,
        msg_cnt: bytes,
        verbosity: Integral = None,
        _locked: bool = False
    ) -> bytes:
        """
        Ask the LEGO EV3 for a reply and wait until it is received

        Positional Arguments
          msg_cnt
            is the message counter of the corresponding send_direct_cmd

        Keyword Arguments
          verbosity
            level (0, 1, 2) of verbosity (prints on stdout).

        Returns
          reply to the direct command (without len, msg_cnt and return status)
        """
        assert isinstance(msg_cnt, bytes), \
            "msg_cnt needs to be of type bytes"
        assert len(msg_cnt) == 2, \
            "msg_cnt must be 2 bytes long"
        assert verbosity is None or isinstance(verbosity, Integral), \
            "verbosity needs to be of type int"
        assert verbosity is None or verbosity >= 0 and verbosity <= 2, \
            "allowed verbosity values are: 0, 1 or 2"

        if not _locked:
            self._physical_ev3._reply_lock.acquire()

        # reply already in buffer?
        reply = self._physical_ev3._reply_buffer.pop(msg_cnt, None)
        if reply is not None:
            self._physical_ev3._lock.release()
            if reply[4:5] == _DIRECT_REPLY:
                return reply
            raise DirCmdError(
                "direct command {:02X}:{:02X} replied error".format(
                    reply[2],
                    reply[3]
                )
            )

        #  get replies from EV3 device until msg_cnt fits
        while True:
            if self._physical_ev3._protocol in (BLUETOOTH, WIFI):
                reply = self._physical_ev3._socket.recv(1024)
            else:
                reply = bytes(self._physical_ev3._device.read(_EP_IN, 1024, 0))
            len_data = struct.unpack('<H', reply[:2])[0] + 2
            msg_cnt_reply = reply[2:4]
            if (
                    verbosity is not None and verbosity > 0 or
                    verbosity is None and self._verbosity > 0
            ):
                print(
                    datetime.now().strftime('%H:%M:%S.%f') +
                    ' Recv 0x|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[0:2]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[2:4]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[4:5]) +
                    '|', end=''
                )
                if len_data > 5:
                    dat = ':'.join(
                        '{:02X}'.format(byte) for byte in reply[5:len_data]
                    )
                    print(dat + '|')
                else:
                    print()

            if msg_cnt != msg_cnt_reply:
                # does not fit, put reply into buffer
                self._physical_ev3.put_to_reply_buffer(
                    msg_cnt_reply,
                    reply[:len_data]
                )
            else:
                self._physical_ev3._lock.release()
                if reply[4:5] == _DIRECT_REPLY:
                    return reply[5:len_data]
                raise DirCmdError(
                    "direct command {:02X}:{:02X} replied error".format(
                        reply[2],
                        reply[3]
                    )
                )

    def send_system_cmd(
        self,
        cmd: bytes,
        reply: bool = True,
        verbosity: Integral = None
    ) -> bytes:
        """
        Send a system command to the LEGO EV3

        Positional Arguments
          cmd
            holds netto data only (cmd and arguments), these fields are added:
              length: 2 bytes, little endian

              msg_cnt: 2 bytes, little endian

              type: 1 byte, SYSTEM_COMMAND_REPLY or SYSTEM_COMMAND_NO_REPLY

        Keyword Arguments
          reply
            flag if with reply
          verbosity
            level (0, 1, 2) of verbosity (prints on stdout).

        Returns
          reply (in case of SYSTEM_COMMAND_NO_REPLY: msg_cnt)
        """
        assert isinstance(cmd, bytes), \
            "cmd needs to be of type bytes"
        assert isinstance(reply, bool), \
            "reply needs to be of type bool"
        assert verbosity is None or isinstance(verbosity, Integral), \
            "verbosity needs to be of type int"
        assert verbosity is None or verbosity >= 0 and verbosity <= 2, \
            "allowed verbosity values are: 0, 1 or 2"

        self._physical_ev3._lock.acquire()

        if reply:
            cmd_type = _SYSTEM_COMMAND_REPLY
        else:
            cmd_type = _SYSTEM_COMMAND_NO_REPLY
        msg_cnt = self._physical_ev3.next_msg_cnt()
        cmd = b''.join([
            struct.pack('<hh', len(cmd) + 3, msg_cnt),
            cmd_type,
            cmd
        ])
        if self._verbosity >= 1:
            print(
                datetime.now().strftime('%H:%M:%S.%f') +
                ' Sent 0x|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[0:2]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[2:4]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[4:5]) + '|' +
                ':'.join('{:02X}'.format(byte) for byte in cmd[5:]) + '|'
            )
        if self._physical_ev3._protocol in (BLUETOOTH, WIFI):
            self._physical_ev3._socket.send(cmd)
        else:
            self._physical_ev3._device.write(_EP_OUT, cmd, 100)

        msg_cnt = cmd[2:4]
        if not reply:
            self._physical_ev3._lock.release()
            return msg_cnt
        else:
            return self._wait_for_system_reply(
                msg_cnt,
                verbosity=verbosity,
                _locked=True
            )

    def _wait_for_system_reply(
            self,
            msg_cnt: bytes,
            verbosity: Integral = None,
            _locked=False
    ) -> bytes:
        """
        Ask the LEGO EV3 for a system command reply and wait until received

        Positional Arguments
          msg_cnt
            is the message counter of the corresponding send_system_cmd

        Returns
          reply to the system command
        """
        assert isinstance(msg_cnt, bytes), \
            "msg_cnt needs to be of type bytes"
        assert len(msg_cnt) == 2, \
            "msg_cnt must be 2 bytes long"
        assert verbosity is None or isinstance(verbosity, Integral), \
            "verbosity needs to be of type int"
        assert verbosity is None or verbosity >= 0 and verbosity <= 2, \
            "allowed verbosity values are: 0, 1 or 2"

        if not _locked:
            self._physical_ev3._lock.acquire()

        # reply already in buffer?
        reply = self._physical_ev3._reply_buffer.pop(msg_cnt, None)
        if reply is not None:
            self._physical_ev3._lock.release()
            if reply[4:5] == _SYSTEM_REPLY:
                return reply[6:]
            raise SysCmdError(
                "system command {:02X}:{:02X} replied error".format(
                    reply[2],
                    reply[3]
                )
            )

        #  get replies from EV3 device until msg_cnt fits
        while True:
            if self._physical_ev3._protocol == BLUETOOTH:
                sleep(0.1)
            if self._physical_ev3._protocol in (BLUETOOTH, WIFI):
                reply = self._physical_ev3._socket.recv(1024)
            else:
                reply = bytes(self._physical_ev3._device.read(_EP_IN, 1024, 0))
            len_data = struct.unpack('<H', reply[:2])[0] + 2
            reply_msg_cnt = reply[2:4]

            if (
                    verbosity is not None and verbosity > 0 or
                    verbosity is None and self._verbosity > 0
            ):
                print(
                    datetime.now().strftime('%H:%M:%S.%f') +
                    ' Recv 0x|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[0:2]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[2:4]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[4:5]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[5:6]) +
                    '|' +
                    ':'.join('{:02X}'.format(byte) for byte in reply[6:7]) +
                    '|',
                    end=''
                )
                if len_data > 7:
                    dat = ':'.join(
                        '{:02X}'.format(byte) for byte in reply[7:len_data]
                    )
                    print(dat + '|')
                else:
                    print()

            if msg_cnt != reply_msg_cnt:
                self._physical_ev3.put_to_reply_buffer(
                    reply_msg_cnt,
                    reply[:len_data]
                )
            else:
                self._physical_ev3._lock.release()
                if reply[4:5] == _SYSTEM_REPLY:
                    return reply[6:len_data]
                raise SysCmdError(
                    "system command {:02X}:{:02X} replied error".format(
                        reply[2],
                        reply[3]
                    )
                )
