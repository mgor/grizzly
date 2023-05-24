from typing import Union
import logging


def tohex(value: Union[int, str, bytes, bytearray]) -> str:
    logging.info(f'{value=}, ({type(value)})')

    if isinstance(value, str):
        return ''.join('{:02x}'.format(ord(c)) for c in value)
    elif isinstance(value, (bytes, bytearray,)):
        return value.hex()
    elif isinstance(value, int):
        return hex(value)[2:]
    else:
        raise ValueError(f'{value} has an unsupported type {type(value)}')
