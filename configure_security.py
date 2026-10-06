"""Run locally once to prepare Render environment values. Never writes secrets."""
import getpass
import secrets

from werkzeug.security import generate_password_hash


def main():
    password = getpass.getpass('New private portfolio password (input hidden): ')
    if len(password) < 12:
        raise SystemExit('Use a password of at least 12 characters.')
    if password != getpass.getpass('Confirm password (input hidden): '):
        raise SystemExit('Passwords do not match.')
    print('\nCopy these values into Render > Environment, then redeploy.')
    print('Do not commit or share these values.')
    print('APP_PASSWORD_HASH=' + generate_password_hash(password))
    print('FLASK_SECRET_KEY=' + secrets.token_hex(32))


if __name__ == '__main__':
    main()