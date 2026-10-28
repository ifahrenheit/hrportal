"""Mailing list sheet sync: cell parsing and add/remove planning. Fully
mocked — no cPanel, Mailman or database calls."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import mailing_list_sheet_sync as s
from modules import mailing_lists as ml

GROUP = "gygcc3@cohere.ph"


def row(email, status="Active", cell="", group="CS"):
    return {"email": email, "status": status, "mailing_list": cell, "group_name": group,
            "schedule_name": email.split("@")[0]}


class ParseCellTests(unittest.TestCase):
    def test_splits_and_lowercases(self):
        self.assertEqual(s.parse_cell("gygcc3@cohere.ph, GYGCCALL@cohere.ph")[0],
                         {"gygcc3@cohere.ph", "gygccall@cohere.ph"})

    def test_alias_and_ignored(self):
        self.assertEqual(s.parse_cell("gygL2@cohere.ph, gygccall@cohere.ph")[0],
                         {"l2-agents@cohere.ph", "gygccall@cohere.ph"})
        self.assertEqual(s.parse_cell("soms@cohere.ph, gygccall@cohere.ph")[0], {"gygccall@cohere.ph"})
        self.assertEqual(s.parse_cell("hr@cohere.ph, gygccall@cohere.ph")[0], {"gygccall@cohere.ph"})

    def test_blank_and_na_give_nothing(self):
        self.assertEqual(s.parse_cell(""), (set(), []))
        self.assertEqual(s.parse_cell(None), (set(), []))
        self.assertEqual(s.parse_cell("#N/A"), (set(), []))

    def test_junk_reported(self):
        groups, bad = s.parse_cell("gygcc3@cohere.ph, gygcc4")
        self.assertEqual(groups, {"gygcc3@cohere.ph"})
        self.assertEqual(bad, ["gygcc4"])


class PlanTests(unittest.TestCase):
    def plan(self, rows, current):
        people, _ = s.build_people(rows)
        return s.plan_group(GROUP, people, current)

    def test_adds_assigned_active_employee(self):
        self.assertEqual(self.plan([row("a@cohere.ph", cell=GROUP)], []), (["a@cohere.ph"], []))

    def test_removes_separated_member(self):
        self.assertEqual(self.plan([row("a@cohere.ph", "Separated")], ["a@cohere.ph"]), ([], ["a@cohere.ph"]))

    def test_removes_member_moved_to_another_group(self):
        self.assertEqual(self.plan([row("a@cohere.ph", cell="gygcc4@cohere.ph")], ["A@cohere.ph"]),
                         ([], ["a@cohere.ph"]))

    def test_blank_cell_left_alone(self):
        self.assertEqual(self.plan([row("a@cohere.ph")], ["a@cohere.ph"]), ([], []))
        self.assertEqual(self.plan([row("a@cohere.ph", cell="#N/A")], ["a@cohere.ph"]), ([], []))

    def test_address_outside_sheet_never_touched(self):
        self.assertEqual(self.plan([], ["manager@cohere.ph", "someone@gmail.com"]), ([], []))

    def test_separated_never_added(self):
        self.assertEqual(self.plan([row("a@cohere.ph", "Separated", GROUP)], []), ([], []))

    def test_rehire_active_row_wins(self):
        rows = [row("a@cohere.ph", "Separated"), row("a@cohere.ph", "Active", GROUP)]
        self.assertEqual(self.plan(rows, ["a@cohere.ph"]), ([], []))

    def test_add_only_group_not_removed_when_moved(self):
        self.assertEqual(self.plan([row("m@cohere.ph", cell="soms@cohere.ph, gygccall@cohere.ph", group="Finest")],
                                   ["m@cohere.ph"]), ([], []))

    def test_add_only_group_still_added(self):
        self.assertEqual(self.plan([row("m@cohere.ph", cell=GROUP, group="Finest")], []), (["m@cohere.ph"], []))

    def test_add_only_group_removed_when_separated(self):
        self.assertEqual(self.plan([row("m@cohere.ph", "Separated", group="Finest")], ["m@cohere.ph"]),
                         ([], ["m@cohere.ph"]))

    def test_removal_guard(self):
        self.assertFalse(s.removal_blocked(2, 20))
        self.assertTrue(s.removal_blocked(6, 20))
        self.assertTrue(s.removal_blocked(s.MAX_REMOVE + 1, 1000))


class ActorTests(unittest.TestCase):
    def test_actor_outside_request_is_sheet_sync(self):
        self.assertEqual(ml._actor(), ("system:sheet-sync", "Sheet sync", None))


if __name__ == "__main__":
    unittest.main()
