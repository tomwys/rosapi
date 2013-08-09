import binascii
import hashlib
import logging
import socket

import socket_utils

logger = logging.getLogger(__name__)


class RosAPIError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        if isinstance(self.value, dict) and self.value.get('message'):
            return self.value['message']
        else:
            return self.value


class RosAPIFatalError(RosAPIError):
    pass


class RosAPI(object):
    """Routeros api"""

    def __init__(self, socket):
        self.socket = socket

    def login(self, username, pwd):
        for _, attrs in self.talk(['/login']):
            token = binascii.unhexlify(attrs['ret'])
        hasher = hashlib.md5()
        hasher.update('\x00')
        hasher.update(pwd)
        hasher.update(token)
        self.talk(['/login', '=name=' + username,
                   '=response=00' + hasher.hexdigest()])

    def talk(self, words):
        if self.write_sentence(words) == 0:
            return
        output = []
        while True:
            input_sentence = self.read_sentence()
            if not len(input_sentence):
                continue
            attrs = {}
            reply = input_sentence.pop(0)
            for line in input_sentence:
                try:
                    second_eq_pos = line.index('=', 1)
                except IndexError:
                    attrs[line[1:]] = ''
                else:
                    attrs[line[1:second_eq_pos]] = line[second_eq_pos + 1:]
            output.append((reply, attrs))
            if reply == '!done':
                if output[0][0] == '!trap':
                    raise RosAPIError(output[0][1])
                if output[0][0] == '!fatal':
                    self.socket.close()
                    raise RosAPIFatalError(output[0][1])
                return output

    def write_sentence(self, words):
        words_written = 0
        for word in words:
            self.write_word(word)
            words_written += 1
        self.write_word('')
        return words_written

    def read_sentence(self):
        sentence = []
        while True:
            word = self.read_word()
            if not len(word):
                return sentence
            sentence.append(word)

    def write_word(self, word):
        logger.debug('>>> %s' % word)
        self.write_lenght(len(word))
        self.write_string(word)

    def read_word(self):
        word = self.read_string(self.read_length())
        logger.debug('<<< %s' % word)
        return word

    def write_lenght(self, length):
        self.write_string(self.length_to_string(length))

    def length_to_string(self, length):
        if length < 0x80:
            return chr(length)
        elif length < 0x4000:
            length |= 0x8000
            return self._pack(2, length)
        elif length < 0x200000:
            length |= 0xC00000
            return self._pack(3, length)
        elif length < 0x10000000:
            length |= 0xE0000000
            return self._pack(4, length)
        else:
            return chr(0xF0) + self._pack(4, length)

    @staticmethod
    def _pack(times, length):
        output = ''
        while times:
            output = chr(length & 0xFF) + output
            times -= 1
            length >>= 8
        return output

    def read_length(self):
        i = ord(self.read_string(1))
        if (i & 0x80) == 0x00:
            pass
        elif (i & 0xC0) == 0x80:
            i &= ~0xC0
            i = self._unpack(1, i)
        elif (i & 0xE0) == 0xC0:
            i &= ~0xE0
            i = self._unpack(2, i)
        elif (i & 0xF0) == 0xE0:
            i &= ~0xF0
            i = self._unpack(3, i)
        elif (i & 0xF8) == 0xF0:
            i = ord(self.read_string(1))
        else:
            raise RosAPIFatalError('Unknown value: %x' % i)
        return i

    def _unpack(self, times, i):
        while times:
            i <<= 8
            i += ord(self.read_string(1))
            times -= 1
        return i

    def write_string(self, string):
        sent_overal = 0
        while sent_overal < len(string):
            try:
                sent = self.socket.send(string[sent_overal:])
            except socket.error as e:
                raise RosAPIFatalError(str(e))
            if sent == 0:
                raise RosAPIFatalError('Connection closed by remote end.')
            sent_overal += sent

    def read_string(self, length):
        received_overal = ''
        while len(received_overal) < length:
            try:
                received = self.socket.recv(length - len(received_overal))
            except socket.error as e:
                raise RosAPIFatalError(str(e))
            if len(received) == 0:
                raise RosAPIFatalError('Connection closed by remote end.')
            received_overal += received
        return received_overal


class RouterboardResource(object):
    def __init__(self, api_client, namespace):
        self.api_client = api_client
        self.namespace = namespace

    def query(self, command, **kwargs):
        command_arguments = self._prepare_arguments(selector_char="?",
                                                    **kwargs)
        return self._send_command(command, command_arguments)

    def call(self, command, *args, **kwargs):
        if "is_query" in kwargs or args: #  TODO remove this after grace period
            logger.warning("'is_query' parameter to RouterboardResource.call"
                           "is deprected and will be removed. For"
                           "is_query=False use .call, for is_query=True use"
                           ".query instead.")
            is_query = kwargs.pop('is_query', False) or any(args)
            if is_query:
                return self.query(command, **kwargs)

        command_arguments = self._prepare_arguments(selector_char="=",
                                                    **kwargs)
        return self._send_command(command, command_arguments)

    def _send_command(self, command, command_arguments):
        response = self.api_client.talk(
            ['%s/%s' % (self.namespace, command)] + command_arguments)
        output = []
        for response_type, attributes in response:
            if response_type == '!re':
                output.append(self._remove_first_char_from_keys(attributes))
        return output

    @staticmethod
    def _prepare_arguments(is_query, selector_char, **kwargs):
        command_arguments = []
        for key, value in kwargs.iteritems():
            if key in ['id', 'proplist']:
                key = '.%s' % key
            key = key.replace('_', '-')
            command_arguments.append(
                '%s%s=%s' % (selector_char, key, value))

        return command_arguments

    @staticmethod
    def _remove_first_char_from_keys(dictionary):
        elements = []
        for key, value in dictionary.iteritems():
            if key in ['.id', '.proplist']:
                key = key[1:]
            elements.append((key, value))
        return dict(elements)

    def get(self, **kwargs):
        return self.query('print', **kwargs)

    def detailed_get(self, **kwargs):
        kwargs['detail'] = kwargs.pop('detail', '')
        return self.call('print', **kwargs)

    def set(self, **kwargs):
        return self.call('set', **kwargs)

    def add(self, **kwargs):
        return self.call('add', **kwargs)

    def remove(self, **kwargs):
        return self.call('remove', **kwargs)


class RouterboardAPI(object):
    port = 8728

    def __init__(self, host, username='api', password=''):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        try:
            sock.connect((host, self.port))
        except socket.error as e:
            raise RosAPIFatalError(str(e))
        socket_utils.set_keepalive(sock, after_idle_sec=10)

        self.api_client = RosAPI(sock)
        self.api_client.login(username, password)

    def get_resource(self, namespace):
        return RouterboardResource(self.api_client, namespace)

    def close_connection(self):
        self.api_client.socket.close()
