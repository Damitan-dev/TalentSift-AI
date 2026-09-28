"""Local/admin-shell account setup. Never pass a password on the command line."""
import argparse
from getpass import getpass

from access_control import AccessStore, password_hash
from database import initialize_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create-recruiter", "reset-recruiter", "password-hash"])
    args = parser.parse_args()
    username = input("Recruiter username: ").strip() if args.command != "password-hash" else None
    password = getpass("Password / passphrase (15–128 characters): ")
    if password != getpass("Confirm password: "):
        parser.error("Passwords do not match.")
    try:
        encoded = password_hash(password)
        if args.command == "password-hash":
            print(encoded)
            return
        initialize_database()
        store = AccessStore()
        store.initialize()
        store.configure_recruiter(username, encoded, replace=args.command == "reset-recruiter")
        print("Recruiter account saved. Sign in at /login. Previous logins were revoked.")
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
