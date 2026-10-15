"""Offline, reversible LAN invitation token. Not encryption and not a security boundary."""
import base64
import ipaddress
import secrets
import socket
import struct
import zlib

PORT = 8765

def local_ipv4():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('192.0.2.1', 80))  # no packet is sent
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return ip
        except OSError:
            pass
    try:
        return next(ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith('127.'))
    except (OSError, StopIteration):
        return '127.0.0.1'

def create_invite(ip, port=PORT):
    addr = ipaddress.IPv4Address(ip)
    if not (1 <= int(port) <= 65535):
        raise ValueError('Invalid port')
    body = bytes(addr.packed) + struct.pack('!H', int(port)) + secrets.token_bytes(5)
    crc = zlib.crc32(body).to_bytes(4, 'big')[:2]
    return base64.b32encode(body + crc).decode('ascii').rstrip('=')

def decode_invite(key):
    token = ''.join(str(key).upper().split()).replace('-', '')
    if len(token) != 21 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in token):
        raise ValueError('邀请码格式无效（应为 21 位字母数字）')
    try:
        raw = base64.b32decode(token + '=' * ((-len(token)) % 8), casefold=True)
    except Exception as exc:
        raise ValueError('邀请码无法解码') from exc
    if len(raw) != 13 or zlib.crc32(raw[:11]).to_bytes(4, 'big')[:2] != raw[11:]:
        raise ValueError('邀请码校验失败')
    ip = str(ipaddress.IPv4Address(raw[:4]))
    port = struct.unpack('!H', raw[4:6])[0]
    if port == 0:
        raise ValueError('非法端口')
    return f'ws://{ip}:{port}', token
