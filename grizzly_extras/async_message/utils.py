def string2hex(value: str) -> str:
    return ''.join('{:02x}'.format(ord(c)) for c in value)
