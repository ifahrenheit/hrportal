"""Numa Stats Teams page: parsing the "Jareth SLA Copy" Looker blocks and the
figures the page shows. Fully mocked: no Google or database calls."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import numa_team_stats as nts
from modules import numa_stats as mod

SLA_TAB = [
    ['Email SLA - Weekly', '', '', '', '', '', 'Email SLA - Monthly'],
    ['Look Url: x', '', '', '', '', '', 'Look Url: y'],
    ['Team Attribute', 'Activity Period', 'Email Message SLA Met', 'Email Message SLA Not Met', 'Email Message SLA Percentage', '',
     'Team Attribute', 'Activity Period', 'Email Message SLA Met', 'Email Message SLA Not Met', 'Email Message SLA Percentage'],
    ['Team Cohere', '2026-11-16', '49', '109', '0.3101265823', '', 'Team Cohere', '2026-11', '655', '1223', '0.3487752929'],
    ['Host', '2026-11-16', '139', '205', '0.40', '', 'GX Team Night Shift', '2026-11', '567', '1,643', '0.256561086'],
]
CONV_TAB = [
    ['Look Url: z'],
    ['Team Attribute', 'Conversation Type', 'Activity Period', 'Unique Conversation Count', 'total', 'year'],
    ['Team Cohere', 'whatsapp threads', '2026', '72', '72', '1905'],
    ['Team Cohere', 'email', '2026', '1991', '1459', '1905'],
]
CSAT_WEEK = [
    ['Team Attribute', 'Feedback Period', 'Agent + Emma CSAT', 'Filtered front_conversations_bq.weighted_q1', 'Avg Csat'],
    ['GX Team Night Shift', '2026-11-16', '0.6666666364', '0.6363636364', '5'],
]


class ParseTests(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(nts.parse_period('2026-11-16'), ('week', date(2026, 11, 16)))
        self.assertEqual(nts.parse_period('2026-11'), ('month', date(2026, 11, 1)))
        self.assertEqual(nts.parse_period('2026'), ('year', date(2026, 1, 1)))
        self.assertEqual(nts.parse_period('Nov'), (None, None))

    def test_metric_keys(self):
        self.assertEqual(nts.metric_key('Agent + Emma CSAT'), 'agent_plus_emma_csat')
        self.assertEqual(nts.metric_key('Email Conversations With Outbound '), 'email_conversations_with_outbound')
        self.assertEqual(nts.metric_key('Filtered front_conversations_bq.weighted_q1'), 'escalated_q1')

    def test_side_by_side_blocks_and_team_filter(self):
        rows = nts.parse_tab(SLA_TAB)
        teams = {r[3] for r in rows}
        self.assertEqual(teams, {'GX Team', 'GX Night Shift'})          # Host dropped
        got = {(r[1], r[2], r[3], r[4]): r[5] for r in rows}
        self.assertEqual(got[('month', date(2026, 11, 1), 'GX Team', 'email_message_sla_met')], 655)
        self.assertEqual(got[('month', date(2026, 11, 1), 'GX Night Shift', 'email_message_sla_not_met')], 1643)
        self.assertAlmostEqual(got[('week', date(2026, 11, 16), 'GX Team', 'email_message_sla_percentage')], 0.3101265823)
        self.assertTrue(all(r[0] == 'email_sla' for r in rows))

    def test_conversation_types_prefix_and_filler_column_dropped(self):
        rows = nts.parse_tab(CONV_TAB)
        metrics = {r[4]: r[5] for r in rows}
        self.assertEqual(metrics['email.unique_conversation_count'], 1991)
        self.assertEqual(metrics['whatsapp_threads.total'], 72)
        self.assertFalse(any(m.endswith('.year') for m in metrics))
        self.assertTrue(all(r[1] == 'year' for r in rows))

    def test_csat_block(self):
        rows = nts.parse_tab(CSAT_WEEK)
        self.assertEqual({r[4] for r in rows}, {'agent_plus_emma_csat', 'escalated_q1', 'avg_csat'})
        self.assertEqual(rows[0][0], 'csat')


class FiguresTests(unittest.TestCase):
    def test_month_figures(self):
        f = mod.team_figures({
            'email.unique_conversation_count': 1991, 'whatsapp.unique_conversation_count': 950,
            'whatsapp_threads.unique_conversation_count': 72, 'sms.unique_conversation_count': 10,
            'email_message_sla_percentage': 0.3487752929, 'email_message_sla_met': 655,
            'agent_csat': 0.918239, 'agent_plus_emma_csat': 0.9, 'avg_csat': 4.5})
        self.assertEqual(f['conversations'], 3023)
        self.assertEqual(f['email_sla_pct'], 34.88)
        self.assertEqual(f['csat_pct'], 91.82)
        self.assertEqual(f['csat_label'], 'Agent CSAT')
        self.assertIsNone(f['chatbot_sla_pct'])

    def test_weekly_falls_back_to_agent_plus_emma(self):
        f = mod.team_figures({'agent_plus_emma_csat': 0.6666666364})
        self.assertEqual((f['csat_pct'], f['csat_label']), (66.67, 'Agent + Emma CSAT'))
        self.assertIsNone(f['conversations'])

    def test_empty(self):
        self.assertIsNone(mod.team_figures(None))
        self.assertIsNone(mod.team_figures({}))

    def test_year_label(self):
        self.assertEqual(mod.team_period_label('year', date(2026, 1, 1)), '2026')


if __name__ == '__main__':
    unittest.main()
