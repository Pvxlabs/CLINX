"""JSON Schema checks require the development-only jsonschema package."""
import copy
import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, FormatChecker
import test_observer_server as fixtures


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(Path("docs/monitor/observer-v1.schema.json").read_text())
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())

    def test_schema_and_all_wire_examples(self):
        Draft202012Validator.check_schema(self.schema)
        examples = json.loads(Path("docs/monitor/api-examples.json").read_text())
        self.assertTrue(examples["synthetic_fixture"])
        for example in examples["examples"]:
            with self.subTest(example=example["name"]):
                self.validator.validate(example["response"])

    def test_live_fixture_matches_schema(self):
        fixture = fixtures.ObserverTests()
        fixture.setUp()
        try:
            fixture.claim()
            self.validator.validate(fixture.store.task(fixture.ref))
            self.validator.validate(fixture.store.list_tasks("active"))
            self.validator.validate(fixture.store.task_events(fixture.ref, None))
            self.validator.validate(fixture.store.health())
        finally:
            fixture.doCleanups()

    def test_reject_leaked_field_and_invented_progress(self):
        examples = json.loads(Path("docs/monitor/api-examples.json").read_text())
        task = copy.deepcopy(next(e["response"] for e in examples["examples"] if e["name"] == "running_detail"))
        task["raw_terminal"] = "private"
        self.assertTrue(list(self.validator.iter_errors(task)))
        del task["raw_terminal"]
        task["progress_percent"] = 42
        self.assertTrue(list(self.validator.iter_errors(task)))