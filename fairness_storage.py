from pathlib import Path
import json
from config import DATA_DIR

class FairnessTestRepo:
    """
    Store and load controlled fairness experiments.

    Runtime data lives in:

        data/fairness_tests.json

    The recruiter fairness dashboard reads this file
    whenever the page is requested.
    """

    def __init__(
    self,
    path: str | Path | None = None,
    ):
        if path is None:

            self.path = (
                DATA_DIR
                / "fairness_tests.json"
            )

        else:

            self.path = Path(
                path
            )

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )


    def load_all(self) -> list[dict]:
        """
        Return every saved fairness experiment.

        Missing or empty files are treated as an
        empty experiment store rather than an error.
        """

        if not self.path.exists():

            return []


        raw_text = self.path.read_text(
            encoding="utf-8"
        ).strip()


        # Your current fairness_tests.json is empty.
        #
        # json.loads("") would crash, so an empty
        # file deliberately means "no tests yet".
        if not raw_text:

            return []


        data = json.loads(
            raw_text
        )


        tests = data.get(
            "tests",
            [],
        )


        if not isinstance(
            tests,
            list,
        ):

            raise ValueError(
                "fairness_tests.json must contain "
                'a "tests" list.'
            )


        return tests


    def save_all(
        self,
        tests: list[dict],
    ) -> Path:
        """
        Save all fairness experiments.

        Write to a temporary file first, then replace
        the real file. This avoids the dashboard reading
        a half-written JSON file.
        """

        payload = {
            "tests": tests,
        }


        temporary_path = (
            self.path.with_suffix(
                ".tmp"
            )
        )


        temporary_path.write_text(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


        temporary_path.replace(
            self.path
        )


        return self.path


    def upsert(
        self,
        fairness_test: dict,
    ) -> Path:
        """
        Add a new experiment or replace an existing
        experiment with the same ID.

        "Upsert" means:
            update if it exists
            insert if it does not
        """

        test_id = fairness_test.get(
            "id"
        )


        if not test_id:

            raise ValueError(
                "A fairness test requires an id."
            )


        tests = self.load_all()


        updated_tests = []

        replaced = False


        for existing_test in tests:

            if (
                existing_test.get("id")
                == test_id
            ):

                updated_tests.append(
                    fairness_test
                )

                replaced = True

            else:

                updated_tests.append(
                    existing_test
                )


        if not replaced:

            updated_tests.append(
                fairness_test
            )


        return self.save_all(
            updated_tests
        )