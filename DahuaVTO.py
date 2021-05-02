#!/usr/bin/env python3

import os
import sys
import logging
import json
import asyncio
import hashlib
from threading import Timer
from time import sleep
from typing import Optional
import paho.mqtt.client as mqtt
import requests
from requests.auth import HTTPDigestAuth

from Messages import MessageData

DEBUG = os.environ.get('DEBUG', False)

log_level = logging.DEBUG if DEBUG else logging.INFO

root = logging.getLogger()
root.setLevel(log_level)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(log_level)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

_LOGGER = logging.getLogger(__name__)

DAHUA_ALLOWED_DETAILS = ["deviceType", "serialNumber"]

ENDPOINT_ACCESS_CONTROL = "accessControl.cgi?action=openDoor&UserID=101&Type=Remote&channel="
ENDPOINT_MAGICBOX_SYSINFO = "magicBox.cgi?action=getSystemInfo"

MQTT_ERROR_DEFAULT_MESSAGE = "Unknown error"

MQTT_ERROR_MESSAGES = {
    1: "MQTT Broker failed to connect: incorrect protocol version",
    2: "MQTT Broker failed to connect: invalid client identifier",
    3: "MQTT Broker failed to connect: server unavailable",
    4: "MQTT Broker failed to connect: bad username or password",
    5: "MQTT Broker failed to connect: not authorised"
}


class DahuaVTOClient(asyncio.Protocol):
    requestId: int
    sessionId: int
    keep_alive_interval: int
    username: str
    password: str
    realm: Optional[str]
    random: Optional[str]
    messages: []
    mqtt_client: mqtt.Client
    dahua_details: {}
    base_url: str

    def __init__(self):
        self.dahua_details = {}
        self.host = os.environ.get('DAHUA_VTO_HOST')
        self.base_url = f"http://{self.host}/cgi-bin/"

        self.username = os.environ.get('DAHUA_VTO_USERNAME')
        self.password = os.environ.get('DAHUA_VTO_PASSWORD')

        self.mqtt_broker_host = os.environ.get('MQTT_BROKER_HOST')
        self.mqtt_broker_port = os.environ.get('MQTT_BROKER_PORT')
        self.mqtt_broker_username = os.environ.get('MQTT_BROKER_USERNAME')
        self.mqtt_broker_password = os.environ.get('MQTT_BROKER_PASSWORD')

        self.mqtt_broker_topic_prefix = os.environ.get('MQTT_BROKER_TOPIC_PREFIX')
        self.mqtt_open_door_topic = f"{self.mqtt_broker_topic_prefix}/Command/Open"

        self.realm = None
        self.random = None
        self.request_id = 1
        self.sessionId = 0
        self.keep_alive_interval = 0
        self.transport = None

        self.mqtt_client = mqtt.Client(transport="websockets")
        self._loop = asyncio.get_event_loop()

    def initialize_mqtt_client(self):
        _LOGGER.info("Websockets: Connecting MQTT Broker")

        self.mqtt_client.user_data_set(self)

        self.mqtt_client.username_pw_set(self.mqtt_broker_username, self.mqtt_broker_password)

        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect

        self.mqtt_client.connect(self.mqtt_broker_host, int(self.mqtt_broker_port), 60)
        self.mqtt_client.loop_start()

    @staticmethod
    def on_mqtt_connect(client, userdata, flags, rc):
        if rc == 0:
            _LOGGER.info(f"MQTT Broker connected with result code {rc}")
            
            client.subscribe(userdata.mqtt_open_door_topic)

        else:
            error_message = MQTT_ERROR_MESSAGES.get(rc, MQTT_ERROR_DEFAULT_MESSAGE)

            _LOGGER.error(error_message)

            asyncio.get_event_loop().stop()

    @staticmethod
    def on_mqtt_message(client, userdata, msg):
        payload = None if msg.payload is None else msg.payload.decode("utf-8")

        _LOGGER.debug(f"MQTT Message {msg.topic}: {payload}")

        if msg.topic == userdata.mqtt_open_door_topic:
            data = {}

            if payload is not None and len(payload) > 0:
                data = json.loads(payload)

            door_id = data.get("Door", 1)

            userdata.access_control_open_door(door_id)

    @staticmethod
    def on_mqtt_disconnect(client, userdata, rc):
        connected = False

        while not connected:
            try:
                _LOGGER.info(f"MQTT Broker got disconnected trying to reconnect")

                client.connect(userdata.mqtt_broker_host, int(userdata.mqtt_broker_port), 60)
                client.loop_start()

                connected = True

            except Exception as ex:
                exc_type, exc_obj, exc_tb = sys.exc_info()

                _LOGGER.error(f"Failed to reconnect, retry in 60 seconds, error: {ex}, Line: {exc_tb.tb_lineno}")

                sleep(60)

    def connection_made(self, transport):
        _LOGGER.debug("Connection established")

        try:
            self.transport = transport

            self.load_dahua_info()
            self.initialize_mqtt_client()
            self.pre_login()

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}")

    def data_received(self, data):
        try:
            message = self.parse_response(data)
            _LOGGER.debug(f"Data received: {message}")

            message_id = message.get("id")
            params = message.get("params")

            if message_id == 1:
                error = message.get("error")

                if error is not None:
                    self.handle_login_error(error, message, params)

            elif message_id == 2:
                self.handle_login(params)

            else:
                method = message.get("method")

                if method == "client.notifyEventStream":
                    self.handle_notify_event_stream(params)

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle message, error: {ex}, Line: {exc_tb.tb_lineno}")

    def handle_notify_event_stream(self, params):
        try:
            event_list = params.get("eventList")

            for message in event_list:
                code = message.get("Code")

                for k in self.dahua_details:
                    if k in DAHUA_ALLOWED_DETAILS:
                        message[k] = self.dahua_details.get(k)

                topic = f"{self.mqtt_broker_topic_prefix}/{code}/Event"

                _LOGGER.debug(f"Publishing MQTT message {topic}: {message}")

                self.mqtt_client.publish(topic, json.dumps(message, indent=4))

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to handle event, error: {ex}, Line: {exc_tb.tb_lineno}")

    def handle_login_error(self, error, message, params):
        error_message = error.get("message")

        if error_message == "Component error: login challenge!":
            self.random = params.get("random")
            self.realm = params.get("realm")
            self.sessionId = message.get("session")

            self.login()

    def handle_login(self, params):
        keep_alive_interval = params.get("keepAliveInterval")

        if keep_alive_interval is not None:
            self.keep_alive_interval = keep_alive_interval - 5

            Timer(self.keep_alive_interval, self.keep_alive).start()

            self.attach_event_manager()

    def eof_received(self):
        _LOGGER.info('Server sent EOF message')

        self._loop.stop()

    def connection_lost(self, exc):
        _LOGGER.error('server closed the connection')

        self._loop.stop()

    def send(self, message_data: MessageData):
        self.request_id += 1

        message_data.id = self.request_id

        if not self.transport.is_closing():
            self.transport.write(message_data.to_message())

    def pre_login(self):
        _LOGGER.debug("Prepare pre-login message")

        message_data = MessageData(self.request_id, self.sessionId)
        message_data.login(self.username)

        if not self.transport.is_closing():
            self.transport.write(message_data.to_message())

    def login(self):
        _LOGGER.debug("Prepare login message")

        password = self._get_hashed_password(self.random, self.realm, self.username, self.password)

        message_data = MessageData(self.request_id, self.sessionId)
        message_data.login(self.username, password)

        self.send(message_data)

    def attach_event_manager(self):
        _LOGGER.info("Attach event manager")

        message_data = MessageData(self.request_id, self.sessionId)
        message_data.attach()

        self.send(message_data)

    def keep_alive(self):
        _LOGGER.debug("Keep alive")

        message_data = MessageData(self.request_id, self.sessionId)
        message_data.keep_alive(self.keep_alive_interval)

        self.send(message_data)

        Timer(self.keep_alive_interval, self.keep_alive).start()

    def load_dahua_info(self):
        try:
            _LOGGER.debug("Loading Dahua details")

            url = f"{self.base_url}{ENDPOINT_MAGICBOX_SYSINFO}"

            response = requests.get(url, auth=HTTPDigestAuth(self.username, self.password))

            response.raise_for_status()

            lines = response.text.split("\r\n")

            for line in lines:
                if "=" in line:
                    parts = line.split("=")
                    self.dahua_details[parts[0]] = parts[1]

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to retrieve Dahua model, error: {ex}, Line: {exc_tb.tb_lineno}")

    def access_control_open_door(self, door_id: int = 1):
        try:
            _LOGGER.debug("Access Control - Open door")

            url = f"{self.base_url}{ENDPOINT_ACCESS_CONTROL}{door_id}"

            auth = HTTPDigestAuth(self.username, self.password)

            response = requests.get(url, verify=False, auth=auth)

            response.raise_for_status()

            _LOGGER.info("Access Control - Door was opened")

        except Exception as ex:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to open door, error: {ex}, Line: {exc_tb.tb_lineno}")

    @staticmethod
    def parse_response(response):
        result = None

        try:
            response_parts = str(response).split("\\x00")
            for response_part in response_parts:
                if response_part.startswith("{"):
                    end = response_part.rindex("}") + 1
                    message = response_part[0:end]

                    result = json.loads(message)

        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            _LOGGER.error(f"Failed to read data: {response}, error: {e}, Line: {exc_tb.tb_lineno}")

        return result

    @staticmethod
    def _get_hashed_password(random, realm, username, password):
        password_str = f"{username}:{realm}:{password}"
        password_bytes = password_str.encode('utf-8')
        password_hash = hashlib.md5(password_bytes).hexdigest().upper()

        random_str = f"{username}:{random}:{password_hash}"
        random_bytes = random_str.encode('utf-8')
        random_hash = hashlib.md5(random_bytes).hexdigest().upper()

        return random_hash


class DahuaVTOManager:
    def __init__(self):
        self._host = os.environ.get('DAHUA_VTO_HOST')

    def initialize(self):
        while True:
            try:
                _LOGGER.info("Connecting")

                loop = asyncio.new_event_loop()

                client = loop.create_connection(DahuaVTOClient, self._host, 5000)
                loop.run_until_complete(client)
                loop.run_forever()
                loop.close()

                _LOGGER.warning("Disconnected, will try to connect in 5 seconds")

                sleep(5)

            except Exception as ex:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                line = exc_tb.tb_lineno

                _LOGGER.error(f"Connection failed will try to connect in 30 seconds, error: {ex}, Line: {line}")

                sleep(30)



manager = DahuaVTOManager()
manager.initialize()


