"""Check the installed MAX client and optional TLS/handshake, WITHOUT requesting SMS.

Run inside Docker: python -m maxost.diagnose [--offline]
No phone, bot token or stored user session is used.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from importlib.metadata import version

from .auth_diagnostics import describe_failure, safe_error_code
from .max_client import NoInteractiveLogin


async def check(offline=False):
    from pymax import Client, ExtraConfig
    client = Client(phone='', work_dir='/tmp', auth_flow=NoInteractiveLogin(),
                    extra_config=ExtraConfig(persist_session=False, reconnect=False,
                                             relogin=False, log_level='CRITICAL'))
    for name, logger in logging.Logger.manager.loggerDict.items():
        if name.startswith('pymax') and isinstance(logger, logging.Logger):
            logger.disabled = True
    stage = 'client_config'
    try:
        await client._ensure_runtime()  # Pinned SDK; local catalog only, no login.
        print(f'PyMax {version("maxapi-python")}; client {client.app_version}; configuration OK')
        if offline:
            print('Offline check only. No connection or SMS request was made.')
            return 0
        stage = 'connecting'
        async with asyncio.timeout(30):
            await client._connection.open()
            response = await client._app.handshake(client._config.device.device_id)
        if response.calls_seed is None:
            raise ValueError('Handshake is incompatible with SMS authentication')
        print('MAX connection and handshake OK. No AUTH_REQUEST/login was sent; SMS delivery was not tested.')
        return 0
    except Exception as exc:
        category, text = describe_failure(exc, stage)
        print(f'MAX check failed: stage={stage} category={category} code={safe_error_code(exc)}')
        print(text)
        return 1
    finally:
        await client.close()


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    # Third-party failures must not dump transport frames or exception strings.
    logging.disable(logging.CRITICAL)
    try:
        code = asyncio.run(check(args.offline))
    except Exception as exc:
        category, text = describe_failure(exc, 'client_config')
        print(f'MAX check failed: category={category}')
        print(text)
        code = 1
    raise SystemExit(code)


if __name__ == '__main__':
    run()
