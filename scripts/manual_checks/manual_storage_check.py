import argparse

from storage import SessionRepo


def main():
    parser = argparse.ArgumentParser(
        description="Load and inspect a stored TalentSift interview session."
    )

    parser.add_argument(
        "session_id",
        help="Session ID to load from storage, without the .json extension.",
    )

    args = parser.parse_args()

    repo = SessionRepo()
    loaded_session = repo.load(args.session_id)

    print("Loaded session:")
    print(loaded_session)

    print("\nObject type:")
    print(type(loaded_session))

    print("\nLanguage:")
    print(loaded_session.language)

    if loaded_session.transcript:
        print("\nFirst transcript turn:")
        print("Speaker:", loaded_session.transcript[0].speaker)
        print("Text:", loaded_session.transcript[0].text)
    else:
        print("\nSession contains no transcript turns.")


if __name__ == "__main__":
    main()