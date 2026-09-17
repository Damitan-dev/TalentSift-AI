# database.py

import sqlite3
# sqlite3 is built into Python.
# We do NOT need to install another package.

from pathlib import Path
# Path helps us safely create the data folder
# and database filename.

from datetime import datetime
# We need this to turn stored timestamp text
# back into a Python datetime.

from models import (
    Candidate,
    Session,
    TranscriptTurn,
)
# This is our validated TalentSift Candidate model.

# ---------------------------------------------------------
# DATABASE LOCATION
# ---------------------------------------------------------

DATA_DIR = Path("data")

DATABASE_PATH = DATA_DIR / "talentsift.db"


def get_connection():
    """
    Open a connection to the TalentSift SQLite database.
    """

    # Make sure data/ exists.
    DATA_DIR.mkdir(
        exist_ok=True
    )


    # FIRST create the database connection.
    connection = sqlite3.connect(
        DATABASE_PATH
    )


    # THEN configure how rows are returned.
    #
    # This lets us later write:
    #
    # row["email"]
    #
    # instead of:
    #
    # row[2]
    connection.row_factory = sqlite3.Row


    # Turn foreign-key checks on.
    connection.execute(
        "PRAGMA foreign_keys = ON"
    )


    return connection


def initialize_database():
    """
    Create TalentSift database tables if they
    do not already exist.
    """

    connection = get_connection()
        


    try:

        # -------------------------------------------------
        # CANDIDATES TABLE
        # -------------------------------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY,
                full_name TEXT,
                email TEXT,
                preferred_language TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


        # -------------------------------------------------
        # SESSIONS TABLE
        # -------------------------------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,

                job_id TEXT NOT NULL,

                candidate_id TEXT NOT NULL,

                status TEXT NOT NULL,

                language TEXT NOT NULL,

                consent_given INTEGER NOT NULL,

                created_at TEXT NOT NULL,

                consented_at TEXT NOT NULL,

                started_at TEXT,

                ended_at TEXT,

                FOREIGN KEY(candidate_id)
                    REFERENCES candidates(id)
            )
            """
        )


        # -------------------------------------------------
        # TRANSCRIPT TURNS TABLE
        # -------------------------------------------------

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                session_id TEXT NOT NULL,

                speaker TEXT NOT NULL,

                text TEXT NOT NULL,

                timestamp TEXT NOT NULL,

                item_id TEXT,

                FOREIGN KEY(session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE
            )
            """
        )


        # Actually save the table creation.
        connection.commit()

    finally:

        # Always close the database connection,
        # even if something goes wrong.
        connection.close()


def save_candidate(candidate: Candidate) -> None:
    """
    Save a TalentSift Candidate into SQLite.
    """

    connection = get_connection()

    try:

        connection.execute(
            """
            INSERT INTO candidates (
                id,
                full_name,
                email,
                preferred_language,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                full_name = excluded.full_name,
                email = excluded.email,
                preferred_language = excluded.preferred_language,
                created_at = excluded.created_at
            """,
            (
                candidate.id,
                candidate.full_name,
                candidate.email,
                candidate.preferred_language,

                # SQLite does not have the same datetime
                # object that Python has.
                #
                # Store the timestamp as ISO text.
                candidate.created_at.isoformat(),
            ),
        )

        # Actually save the change to the database file.
        connection.commit()

    finally:

        connection.close()



def load_candidate(
    candidate_id: str
) -> Candidate:
    """
    Load one TalentSift Candidate from SQLite.
    """

    connection = get_connection()

    try:

        row = connection.execute(
            """
            SELECT
                id,
                full_name,
                email,
                preferred_language,
                created_at
            FROM candidates
            WHERE id = ?
            """,
            (
                candidate_id,
            ),
        ).fetchone()


        # No matching candidate?
        if row is None:

            raise KeyError(
                f"Candidate not found: {candidate_id}"
            )


        # Turn the database row back into
        # our normal Pydantic Candidate object.
        return Candidate(
            id=row["id"],
            full_name=row["full_name"],
            email=row["email"],
            preferred_language=(
                row["preferred_language"]
            ),

            # We stored the datetime as text.
            #
            # Convert it back into a real
            # Python datetime object.
            created_at=datetime.fromisoformat(
                row["created_at"]
            ),
        )

    finally:

        connection.close()



def save_session(session: Session) -> None:
    """
    Save one TalentSift interview Session into SQLite.
    """

    connection = get_connection()

    try:

        connection.execute(
            """
            INSERT INTO sessions (
                id,
                job_id,
                candidate_id,
                status,
                language,
                consent_given,
                created_at,
                consented_at,
                started_at,
                ended_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(id) DO UPDATE SET
                job_id = excluded.job_id,
                candidate_id = excluded.candidate_id,
                status = excluded.status,
                language = excluded.language,
                consent_given = excluded.consent_given,
                created_at = excluded.created_at,
                consented_at = excluded.consented_at,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at
            """,
            (
                session.id,
                session.job_id,
                session.candidate_id,
                session.status,
                session.language,

                # SQLite does not have a true Boolean type.
                #
                # We store:
                # False -> 0
                # True  -> 1
                int(session.consent_given),

                session.created_at.isoformat(),

                session.consented_at.isoformat(),

                # These two can be None.
                session.started_at.isoformat()
                if session.started_at
                else None,

                session.ended_at.isoformat()
                if session.ended_at
                else None,
            ),
        )


        connection.commit()

    finally:

        connection.close()



def load_session(
    session_id: str
) -> Session:
    """
    Load one TalentSift Session from SQLite.
    """

    connection = get_connection()

    try:

        row = connection.execute(
            """
            SELECT
                id,
                job_id,
                candidate_id,
                status,
                language,
                consent_given,
                created_at,
                consented_at,
                started_at,
                ended_at
            FROM sessions
            WHERE id = ?
            """,
            (
                session_id,
            ),
        ).fetchone()


        if row is None:

            raise KeyError(
                f"Session not found: {session_id}"
            )


        return Session(
            id=row["id"],

            job_id=row["job_id"],

            candidate_id=row["candidate_id"],

            status=row["status"],

            language=row["language"],

            # SQLite gives us 0 or 1.
            # bool() turns that back into
            # False or True.
            consent_given=bool(
                row["consent_given"]
            ),

            created_at=datetime.fromisoformat(
                row["created_at"]
            ),

            consented_at=datetime.fromisoformat(
                row["consented_at"]
            ),

            started_at=(
                datetime.fromisoformat(
                    row["started_at"]
                )
                if row["started_at"]
                else None
            ),

            ended_at=(
                datetime.fromisoformat(
                    row["ended_at"]
                )
                if row["ended_at"]
                else None
            ),

            # We have not connected transcript_turns
            # to load_session yet.
            #
            # That is the NEXT database step.
            transcript=load_transcript_turns(
                    session_id
                ),
        )

    finally:

        connection.close()


def save_transcript_turn(
    session_id: str,
    turn: TranscriptTurn,
) -> None:
    """
    Save one completed transcript turn
    for one interview session.
    """

    connection = get_connection()

    try:

        connection.execute(
            """
            INSERT INTO transcript_turns (
                session_id,
                speaker,
                text,
                timestamp,
                item_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn.speaker,
                turn.text,
                turn.timestamp.isoformat(),
                turn.item_id,
            ),
        )

        connection.commit()

    finally:

        connection.close()

def load_transcript_turns(
    session_id: str
) -> list[TranscriptTurn]:
    """
    Load all completed transcript turns
    belonging to one interview session.
    """

    connection = get_connection()

    try:

        rows = connection.execute(
            """
            SELECT
                speaker,
                text,
                timestamp,
                item_id
            FROM transcript_turns
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (
                session_id,
            ),
        ).fetchall()


        turns = []


        for row in rows:

            turn = TranscriptTurn(
                speaker=row["speaker"],

                # Keep the transcript exactly as
                # stored, including punctuation.
                text=row["text"],

                timestamp=datetime.fromisoformat(
                    row["timestamp"]
                ),

                item_id=row["item_id"],
            )


            turns.append(
                turn
            )


        return turns

    finally:

        connection.close()