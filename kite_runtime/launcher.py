"""Authenticate with Kite Connect and start Kite dashboard or MTF alerts."""
import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

from .auth import ensure_session

ROOT = Path(__file__).resolve().parents[1]


def stop_services(processes):
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    deadline = time.monotonic() + 10
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manual-auth', action='store_true', help='Paste redirect URL if the local callback is not configured')
    parser.add_argument('--login', action='store_true', help='Force a fresh login even if the saved token works')
    parser.add_argument('application', choices=('kite', 'mtf'), help='Application to start')
    parser.add_argument('--port', type=int, default=8000, help='Dashboard port (default: 8000)')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    processes = []

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        ensure_session(manual=args.manual_auth, force=args.login)
        if args.application == 'kite':
            processes.append(subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'dashboard:app', '--host', '127.0.0.1', '--port', str(args.port)],
                cwd=ROOT / 'kite', start_new_session=True))
            print(f'Kite dashboard starting at http://127.0.0.1:{args.port} (includes Kite alerts).', flush=True)
        if args.application == 'mtf':
            processes.append(subprocess.Popen(
                [sys.executable, '-m', 'mtf_alert'], cwd=ROOT, start_new_session=True))
            print('MTF alerts starting. Press Ctrl+C to stop.', flush=True)
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    print(f'A service exited (code {code}); stopping the remaining services.', file=sys.stderr)
                    return code if code > 0 else 1
            time.sleep(0.5)
    except (KeyboardInterrupt, EOFError):
        print('\nStopping.')
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Startup failed ({type(exc).__name__}). Check credentials, connectivity and available ports.', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop_services(processes)


if __name__ == '__main__':
    raise SystemExit(main())
