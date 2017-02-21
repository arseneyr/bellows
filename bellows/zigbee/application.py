import asyncio
import logging

import bellows.types as t
import bellows.zigbee.appdb
import bellows.zigbee.device
import bellows.zigbee.util
import bellows.zigbee.zcl
import bellows.zigbee.zdo

LOGGER = logging.getLogger(__name__)


class ControllerApplication(bellows.zigbee.util.ListenableMixin):
    direct = t.EmberOutgoingMessageType.OUTGOING_DIRECT

    def __init__(self, ezsp, database_file=None):
        self._send_sequence = 0
        self._ezsp = ezsp
        self.devices = {}
        self._pending = {}
        self._listeners = {}

        if database_file is not None:
            self._dblistener = bellows.zigbee.appdb.PersistingListener(database_file, self)
            self.add_listener(self._dblistener)
            self._dblistener.load()

    @asyncio.coroutine
    def startup(self):
        e = self._ezsp

        yield from e.reset()
        yield from e.version(4)

        c = t.EzspConfigId
        yield from self._cfg(c.CONFIG_STACK_PROFILE, 2)
        yield from self._cfg(c.CONFIG_SECURITY_LEVEL, 5)
        yield from self._cfg(c.CONFIG_SUPPORTED_NETWORKS, 1)
        yield from self._cfg(c.CONFIG_SUPPORTED_NETWORKS, 1)
        zdo = (
            t.EmberZdoConfigurationFlags.APP_RECEIVES_SUPPORTED_ZDO_REQUESTS |
            t.EmberZdoConfigurationFlags.APP_HANDLES_UNSUPPORTED_ZDO_REQUESTS
        )
        yield from self._cfg(c.CONFIG_APPLICATION_ZDO_FLAGS, zdo)
        yield from self._cfg(c.CONFIG_TRUST_CENTER_ADDRESS_CACHE_SIZE, 2)
        yield from self._cfg(c.CONFIG_PACKET_BUFFER_COUNT, 0xff)

        v = yield from e.networkInit()
        assert v[0] == 0  # TODO: Better check

        v = yield from e.getNetworkParameters()
        assert v[0] == 0  # TODO: Better check
        if v[1] != t.EmberNodeType.COORDINATOR:
            raise Exception("Network not configured as coordinator")

        yield from self._policy()
        nwk = yield from e.getNodeId()
        self._nwk = nwk[0]
        ieee = yield from e.getEui64()
        self._ieee = ieee[0]

        e.add_callback(self.ezsp_callback_handler)

    @asyncio.coroutine
    def _cfg(self, config_id, value):
        v = yield from self._ezsp.setConfigurationValue(config_id, value)
        assert v[0] == 0  # TODO: Better check

    @asyncio.coroutine
    def _policy(self):
        """Set up the policies for what the NCP should do"""
        e = self._ezsp
        v = yield from e.setPolicy(
            t.EzspPolicyId.TC_KEY_REQUEST_POLICY,
            t.EzspDecisionId.DENY_TC_KEY_REQUESTS,
        )
        assert v[0] == 0  # TODO: Better check
        v = yield from e.setPolicy(
            t.EzspPolicyId.APP_KEY_REQUEST_POLICY,
            t.EzspDecisionId.ALLOW_APP_KEY_REQUESTS,
        )
        assert v[0] == 0  # TODO: Better check
        v = yield from e.setPolicy(
            t.EzspPolicyId.TRUST_CENTER_POLICY,
            t.EzspDecisionId.ALLOW_PRECONFIGURED_KEY_JOINS,
        )
        assert v[0] == 0  # TODO: Better check

    def add_device(self, ieee, nwk):
        assert isinstance(ieee, t.EmberEUI64)
        if ieee in self.devices:
            # TODO: Check NWK?
            return self.devices[ieee]
        dev = bellows.zigbee.device.Device(self, ieee, nwk)
        self.devices[ieee] = dev
        return dev

    def ezsp_callback_handler(self, frame_name, args):
        if frame_name == 'incomingMessageHandler':
            self._handle_frame(*args)
        elif frame_name == 'messageSentHandler':
            if args[4] != 0:
                self._handle_frame_failure(*args)
        elif frame_name == 'trustCenterJoinHandler':
            if args[2] == t.EmberDeviceUpdate.DEVICE_LEFT:
                self._handle_leave(*args)
            else:
                self._handle_join(*args)

    def _handle_frame(self, message_type, aps_frame, lqi, rssi, sender, binding_index, address_index, message):
        try:
            self.get_device(nwk=sender).radio_details(lqi, rssi)
        except KeyError:
            LOGGER.debug("No such device %s", sender)

        if aps_frame.destinationEndpoint == 0:
            deserialize = bellows.zigbee.zdo.deserialize
        else:
            deserialize = bellows.zigbee.zcl.deserialize

        tsn, command_id, is_reply, args = deserialize(aps_frame, message)

        if is_reply:
            self._handle_reply(sender, aps_frame, tsn, command_id, args)
        else:
            self._handle_request(sender, aps_frame, tsn, command_id, args)

    def _handle_reply(self, sender, aps_frame, tsn, command_id, args):
        try:
            fut = self._pending.pop(tsn)
            fut.set_result(args)
        except KeyError:
            LOGGER.warning("Unexpected response TSN=%s command=%s args=%s", tsn, command_id, args)

    def _handle_request(self, sender, aps_frame, tsn, command_id, args):
        try:
            device = self.get_device(nwk=sender)
        except KeyError:
            LOGGER.warning("Request on unknown device 0x%04x", sender)
            return

        return device.handle_request(aps_frame, tsn, command_id, args)

    def _handle_join(self, nwk, ieee, device_update, join_dec, parent_nwk):
        LOGGER.info("Device 0x%04x (%s) joined the network", nwk, ieee)
        if ieee in self.devices and self.devices[ieee]._nwk == nwk:
            LOGGER.debug("Skip initialization for existing device %s", ieee)
            return

        dev = self.add_device(ieee, nwk)
        self.listener_event('device_joined', dev)
        loop = asyncio.get_event_loop()
        loop.call_soon(asyncio.async, dev.initialize())

    def _handle_leave(self, nwk, ieee, *args):
        LOGGER.info("Device 0x%04x (%s) left the network", nwk, ieee)
        dev = self.devices.pop(ieee, None)
        if dev is not None:
            self.listener_event('device_left', dev)

    def _handle_frame_failure(self, message_type, destination, aps_frame, message_tag, status, message):
        try:
            fut = self._pending.pop(message_tag)
            fut.set_exception(Exception("Message send failure"))
        except KeyError:
            LOGGER.warning("Unexpected message send failure")

    @asyncio.coroutine
    def request(self, nwk, aps_frame, data):
        seq = aps_frame.sequence
        assert seq not in self._pending
        fut = asyncio.Future()
        self._pending[seq] = fut

        v = yield from self._ezsp.sendUnicast(self.direct, nwk, aps_frame, seq, data)
        if v[0] != 0:
            self._pending.pop(seq)
            raise Exception("Message send failure %s" % (v[0], ))

        v = yield from fut
        return v

    def reply(self, nwk, aps_frame, data):
        return self._ezsp.sendUnicast(self.direct, nwk, aps_frame, aps_frame.sequence, data)

    def permit(self, time_s=60):
        assert 0 <= time_s <= 254
        return self._ezsp.permitJoining(time_s)

    def get_sequence(self):
        self._send_sequence = (self._send_sequence + 1) % 256
        return self._send_sequence

    def get_device(self, ieee=None, nwk=None):
        if ieee is not None:
            return self.devices[ieee]

        for dev in self.devices.values():
            # TODO: Make this not terrible
            if dev._nwk == nwk:
                return dev

        raise KeyError
