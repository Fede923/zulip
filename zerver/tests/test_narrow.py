import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from unittest import mock

import ujson
from django.db import connection
from django.test import override_settings
from sqlalchemy.sql import and_, column, select, table
from sqlalchemy.sql.elements import ClauseElement

from zerver.lib.actions import do_deactivate_user, do_set_realm_property
from zerver.lib.message import MessageDict
from zerver.lib.narrow import build_narrow_filter, is_web_public_compatible
from zerver.lib.request import JsonableError
from zerver.lib.sqlalchemy_utils import get_sqlalchemy_connection
from zerver.lib.streams import create_streams_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import POSTRequestMock, get_user_messages, queries_captured
from zerver.lib.topic import MATCH_TOPIC, TOPIC_NAME
from zerver.lib.topic_mutes import set_topic_mutes
from zerver.lib.types import DisplayRecipientT
from zerver.models import (
    Message,
    Realm,
    Recipient,
    Stream,
    Subscription,
    UserMessage,
    get_display_recipient,
    get_realm,
    get_stream,
)
from zerver.views.message_fetch import (
    LARGER_THAN_MAX_MESSAGE_ID,
    BadNarrowOperator,
    NarrowBuilder,
    Query,
    exclude_muting_conditions,
    find_first_unread_anchor,
    get_messages_backend,
    ok_to_include_history,
    post_process_limited_query,
)


def get_sqlalchemy_sql(query: ClauseElement) -> str:
    dialect = get_sqlalchemy_connection().dialect
    comp = query.compile(dialect=dialect)
    return str(comp)

def get_sqlalchemy_query_params(query: ClauseElement) -> Dict[str, object]:
    dialect = get_sqlalchemy_connection().dialect
    comp = query.compile(dialect=dialect)
    return comp.params

def get_recipient_id_for_stream_name(realm: Realm, stream_name: str) -> str:
    stream = get_stream(stream_name, realm)
    return stream.recipient.id

def mute_stream(realm: Realm, user_profile: str, stream_name: str) -> None:
    stream = get_stream(stream_name, realm)
    recipient = stream.recipient
    subscription = Subscription.objects.get(recipient=recipient, user_profile=user_profile)
    subscription.is_muted = True
    subscription.save()

def first_visible_id_as(message_id: int) -> Any:
    return mock.patch(
        'zerver.views.message_fetch.get_first_visible_message_id',
        return_value=message_id,
    )

class NarrowBuilderTest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm('zulip')
        self.user_profile = self.example_user('hamlet')
        self.builder = NarrowBuilder(self.user_profile, column('id'))
        self.raw_query = select([column("id")], None, table("zerver_message"))
        self.hamlet_email = self.example_user('hamlet').email
        self.othello_email = self.example_user('othello').email

    def test_add_term_using_not_defined_operator(self) -> None:
        term = dict(operator='not-defined', operand='any')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_stream_operator(self) -> None:
        term = dict(operator='stream', operand='Scotland')
        self._do_add_term_test(term, 'WHERE recipient_id = %(recipient_id_1)s')

    def test_add_term_using_stream_operator_and_negated(self) -> None:  # NEGATED
        term = dict(operator='stream', operand='Scotland', negated=True)
        self._do_add_term_test(term, 'WHERE recipient_id != %(recipient_id_1)s')

    def test_add_term_using_stream_operator_and_non_existing_operand_should_raise_error(
            self) -> None:  # NEGATED
        term = dict(operator='stream', operand='NonExistingStream')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_is_operator_and_private_operand(self) -> None:
        term = dict(operator='is', operand='private')
        self._do_add_term_test(term, 'WHERE (flags & %(flags_1)s) != %(param_1)s')

    def test_add_term_using_streams_operator_and_invalid_operand_should_raise_error(
            self) -> None:  # NEGATED
        term = dict(operator='streams', operand='invalid_operands')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_streams_operator_and_public_stream_operand(self) -> None:
        term = dict(operator='streams', operand='public')
        self._do_add_term_test(term, 'WHERE recipient_id IN (%(recipient_id_1)s, %(recipient_id_2)s, %(recipient_id_3)s, %(recipient_id_4)s, %(recipient_id_5)s)')

        # Add new streams
        stream_dicts: List[Mapping[str, Any]] = [
            {
                "name": "publicstream",
                "description": "Public stream with public history",
            },
            {
                "name": "privatestream",
                "description": "Private stream with non-public history",
                "invite_only": True,
            },
            {
                "name": "privatewithhistory",
                "description": "Private stream with public history",
                "invite_only": True,
                "history_public_to_subscribers": True,
            },
        ]
        realm = get_realm('zulip')
        created, existing = create_streams_if_needed(realm, stream_dicts)
        self.assertEqual(len(created), 3)
        self.assertEqual(len(existing), 0)

        # Number of recipient ids will increase by 1 and not 3
        self._do_add_term_test(term, 'WHERE recipient_id IN (%(recipient_id_1)s, %(recipient_id_2)s, %(recipient_id_3)s, %(recipient_id_4)s, %(recipient_id_5)s, %(recipient_id_6)s)')

    def test_add_term_using_streams_operator_and_public_stream_operand_negated(self) -> None:
        term = dict(operator='streams', operand='public', negated=True)
        self._do_add_term_test(term, 'WHERE recipient_id NOT IN (%(recipient_id_1)s, %(recipient_id_2)s, %(recipient_id_3)s, %(recipient_id_4)s, %(recipient_id_5)s)')

        # Add new streams
        stream_dicts: List[Mapping[str, Any]] = [
            {
                "name": "publicstream",
                "description": "Public stream with public history",
            },
            {
                "name": "privatestream",
                "description": "Private stream with non-public history",
                "invite_only": True,
            },
            {
                "name": "privatewithhistory",
                "description": "Private stream with public history",
                "invite_only": True,
                "history_public_to_subscribers": True,
            },
        ]
        realm = get_realm('zulip')
        created, existing = create_streams_if_needed(realm, stream_dicts)
        self.assertEqual(len(created), 3)
        self.assertEqual(len(existing), 0)

        # Number of recipient ids will increase by 1 and not 3
        self._do_add_term_test(term, 'WHERE recipient_id NOT IN (%(recipient_id_1)s, %(recipient_id_2)s, %(recipient_id_3)s, %(recipient_id_4)s, %(recipient_id_5)s, %(recipient_id_6)s)')

    def test_add_term_using_is_operator_private_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='is', operand='private', negated=True)
        self._do_add_term_test(term, 'WHERE (flags & %(flags_1)s) = %(param_1)s')

    def test_add_term_using_is_operator_and_non_private_operand(self) -> None:
        for operand in ['starred', 'mentioned', 'alerted']:
            term = dict(operator='is', operand=operand)
            self._do_add_term_test(term, 'WHERE (flags & %(flags_1)s) != %(param_1)s')

    def test_add_term_using_is_operator_and_unread_operand(self) -> None:
        term = dict(operator='is', operand='unread')
        self._do_add_term_test(term, 'WHERE (flags & %(flags_1)s) = %(param_1)s')

    def test_add_term_using_is_operator_and_unread_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='is', operand='unread', negated=True)
        self._do_add_term_test(term, 'WHERE (flags & %(flags_1)s) != %(param_1)s')

    def test_add_term_using_is_operator_non_private_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='is', operand='starred', negated=True)
        where_clause = 'WHERE (flags & %(flags_1)s) = %(param_1)s'
        params = dict(
            flags_1=UserMessage.flags.starred.mask,
            param_1=0,
        )
        self._do_add_term_test(term, where_clause, params)

        term = dict(operator='is', operand='alerted', negated=True)
        where_clause = 'WHERE (flags & %(flags_1)s) = %(param_1)s'
        params = dict(
            flags_1=UserMessage.flags.has_alert_word.mask,
            param_1=0,
        )
        self._do_add_term_test(term, where_clause, params)

        term = dict(operator='is', operand='mentioned', negated=True)
        where_clause = 'WHERE NOT ((flags & %(flags_1)s) != %(param_1)s OR (flags & %(flags_2)s) != %(param_2)s)'
        params = dict(
            flags_1=UserMessage.flags.mentioned.mask,
            param_1=0,
            flags_2=UserMessage.flags.wildcard_mentioned.mask,
            param_2=0,
        )
        self._do_add_term_test(term, where_clause, params)

    def test_add_term_using_non_supported_operator_should_raise_error(self) -> None:
        term = dict(operator='is', operand='non_supported')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_topic_operator_and_lunch_operand(self) -> None:
        term = dict(operator='topic', operand='lunch')
        self._do_add_term_test(term, 'WHERE upper(subject) = upper(%(param_1)s)')

    def test_add_term_using_topic_operator_lunch_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='topic', operand='lunch', negated=True)
        self._do_add_term_test(term, 'WHERE upper(subject) != upper(%(param_1)s)')

    def test_add_term_using_topic_operator_and_personal_operand(self) -> None:
        term = dict(operator='topic', operand='personal')
        self._do_add_term_test(term, 'WHERE upper(subject) = upper(%(param_1)s)')

    def test_add_term_using_topic_operator_personal_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='topic', operand='personal', negated=True)
        self._do_add_term_test(term, 'WHERE upper(subject) != upper(%(param_1)s)')

    def test_add_term_using_sender_operator(self) -> None:
        term = dict(operator='sender', operand=self.othello_email)
        self._do_add_term_test(term, 'WHERE sender_id = %(param_1)s')

    def test_add_term_using_sender_operator_and_negated(self) -> None:  # NEGATED
        term = dict(operator='sender', operand=self.othello_email, negated=True)
        self._do_add_term_test(term, 'WHERE sender_id != %(param_1)s')

    def test_add_term_using_sender_operator_with_non_existing_user_as_operand(
            self) -> None:  # NEGATED
        term = dict(operator='sender', operand='non-existing@zulip.com')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_pm_with_operator_and_not_the_same_user_as_operand(self) -> None:
        term = dict(operator='pm-with', operand=self.othello_email)
        self._do_add_term_test(term, 'WHERE sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s OR sender_id = %(sender_id_2)s AND recipient_id = %(recipient_id_2)s')

    def test_add_term_using_pm_with_operator_not_the_same_user_as_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='pm-with', operand=self.othello_email, negated=True)
        self._do_add_term_test(term, 'WHERE NOT (sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s OR sender_id = %(sender_id_2)s AND recipient_id = %(recipient_id_2)s)')

    def test_add_term_using_pm_with_operator_the_same_user_as_operand(self) -> None:
        term = dict(operator='pm-with', operand=self.hamlet_email)
        self._do_add_term_test(term, 'WHERE sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s')

    def test_add_term_using_pm_with_operator_the_same_user_as_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='pm-with', operand=self.hamlet_email, negated=True)
        self._do_add_term_test(term, 'WHERE NOT (sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s)')

    def test_add_term_using_pm_with_operator_and_self_and_user_as_operand(self) -> None:
        myself_and_other = ','.join([
            self.example_user('hamlet').email,
            self.example_user('othello').email,
        ])
        term = dict(operator='pm-with', operand=myself_and_other)
        self._do_add_term_test(term, 'WHERE sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s OR sender_id = %(sender_id_2)s AND recipient_id = %(recipient_id_2)s')

    def test_add_term_using_pm_with_operator_more_than_one_user_as_operand(self) -> None:
        two_others = ','.join([
            self.example_user('cordelia').email,
            self.example_user('othello').email,
        ])
        term = dict(operator='pm-with', operand=two_others)
        self._do_add_term_test(term, 'WHERE recipient_id = %(recipient_id_1)s')

    def test_add_term_using_pm_with_operator_self_and_user_as_operand_and_negated(
            self) -> None:  # NEGATED
        myself_and_other = ','.join([
            self.example_user('hamlet').email,
            self.example_user('othello').email,
        ])
        term = dict(operator='pm-with', operand=myself_and_other, negated=True)
        self._do_add_term_test(term, 'WHERE NOT (sender_id = %(sender_id_1)s AND recipient_id = %(recipient_id_1)s OR sender_id = %(sender_id_2)s AND recipient_id = %(recipient_id_2)s)')

    def test_add_term_using_pm_with_operator_more_than_one_user_as_operand_and_negated(self) -> None:
        two_others = ','.join([
            self.example_user('cordelia').email,
            self.example_user('othello').email,
        ])
        term = dict(operator='pm-with', operand=two_others, negated=True)
        self._do_add_term_test(term, 'WHERE recipient_id != %(recipient_id_1)s')

    def test_add_term_using_pm_with_operator_with_comma_noise(self) -> None:
        term = dict(operator='pm-with', operand=' ,,, ,,, ,')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_pm_with_operator_with_existing_and_non_existing_user_as_operand(self) -> None:
        term = dict(operator='pm-with', operand=self.othello_email + ',non-existing@zulip.com')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_id_operator(self) -> None:
        term = dict(operator='id', operand=555)
        self._do_add_term_test(term, 'WHERE id = %(param_1)s')

    def test_add_term_using_id_operator_invalid(self) -> None:
        term = dict(operator='id', operand='')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

        term = dict(operator='id', operand='notanint')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_id_operator_and_negated(self) -> None:  # NEGATED
        term = dict(operator='id', operand=555, negated=True)
        self._do_add_term_test(term, 'WHERE id != %(param_1)s')

    def test_add_term_using_group_pm_operator_and_not_the_same_user_as_operand(self) -> None:
        # Test wtihout any such group PM threads existing
        term = dict(operator='group-pm-with', operand=self.othello_email)
        self._do_add_term_test(term, 'WHERE 1 != 1')

        # Test with at least one such group PM thread existing
        self.send_huddle_message(self.user_profile, [self.example_user("othello"),
                                                     self.example_user("cordelia")])

        term = dict(operator='group-pm-with', operand=self.othello_email)
        self._do_add_term_test(term, 'WHERE recipient_id IN (%(recipient_id_1)s)')

    def test_add_term_using_group_pm_operator_not_the_same_user_as_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='group-pm-with', operand=self.othello_email, negated=True)
        self._do_add_term_test(term, 'WHERE 1 = 1')

    def test_add_term_using_group_pm_operator_with_non_existing_user_as_operand(self) -> None:
        term = dict(operator='group-pm-with', operand='non-existing@zulip.com')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    @override_settings(USING_PGROONGA=False)
    def test_add_term_using_search_operator(self) -> None:
        term = dict(operator='search', operand='"french fries"')
        self._do_add_term_test(term, 'WHERE (content ILIKE %(content_1)s OR subject ILIKE %(subject_1)s) AND (search_tsvector @@ plainto_tsquery(%(param_4)s, %(param_5)s))')

    @override_settings(USING_PGROONGA=False)
    def test_add_term_using_search_operator_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='search', operand='"french fries"', negated=True)
        self._do_add_term_test(term, 'WHERE NOT (content ILIKE %(content_1)s OR subject ILIKE %(subject_1)s) AND NOT (search_tsvector @@ plainto_tsquery(%(param_4)s, %(param_5)s))')

    @override_settings(USING_PGROONGA=True)
    def test_add_term_using_search_operator_pgroonga(self) -> None:
        term = dict(operator='search', operand='"french fries"')
        self._do_add_term_test(term, 'WHERE search_pgroonga &@~ escape_html(%(escape_html_1)s)')

    @override_settings(USING_PGROONGA=True)
    def test_add_term_using_search_operator_and_negated_pgroonga(
            self) -> None:  # NEGATED
        term = dict(operator='search', operand='"french fries"', negated=True)
        self._do_add_term_test(term, 'WHERE NOT (search_pgroonga &@~ escape_html(%(escape_html_1)s))')

    def test_add_term_using_has_operator_and_attachment_operand(self) -> None:
        term = dict(operator='has', operand='attachment')
        self._do_add_term_test(term, 'WHERE has_attachment')

    def test_add_term_using_has_operator_attachment_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='has', operand='attachment', negated=True)
        self._do_add_term_test(term, 'WHERE NOT has_attachment')

    def test_add_term_using_has_operator_and_image_operand(self) -> None:
        term = dict(operator='has', operand='image')
        self._do_add_term_test(term, 'WHERE has_image')

    def test_add_term_using_has_operator_image_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='has', operand='image', negated=True)
        self._do_add_term_test(term, 'WHERE NOT has_image')

    def test_add_term_using_has_operator_and_link_operand(self) -> None:
        term = dict(operator='has', operand='link')
        self._do_add_term_test(term, 'WHERE has_link')

    def test_add_term_using_has_operator_link_operand_and_negated(
            self) -> None:  # NEGATED
        term = dict(operator='has', operand='link', negated=True)
        self._do_add_term_test(term, 'WHERE NOT has_link')

    def test_add_term_using_has_operator_non_supported_operand_should_raise_error(self) -> None:
        term = dict(operator='has', operand='non_supported')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_in_operator(self) -> None:
        mute_stream(self.realm, self.user_profile, 'Verona')
        term = dict(operator='in', operand='home')
        self._do_add_term_test(term, 'WHERE recipient_id NOT IN (%(recipient_id_1)s)')

    def test_add_term_using_in_operator_and_negated(self) -> None:
        # negated = True should not change anything
        mute_stream(self.realm, self.user_profile, 'Verona')
        term = dict(operator='in', operand='home', negated=True)
        self._do_add_term_test(term, 'WHERE recipient_id NOT IN (%(recipient_id_1)s)')

    def test_add_term_using_in_operator_and_all_operand(self) -> None:
        mute_stream(self.realm, self.user_profile, 'Verona')
        term = dict(operator='in', operand='all')
        query = self._build_query(term)
        self.assertEqual(get_sqlalchemy_sql(query), 'SELECT id \nFROM zerver_message')

    def test_add_term_using_in_operator_all_operand_and_negated(self) -> None:
        # negated = True should not change anything
        mute_stream(self.realm, self.user_profile, 'Verona')
        term = dict(operator='in', operand='all', negated=True)
        query = self._build_query(term)
        self.assertEqual(get_sqlalchemy_sql(query), 'SELECT id \nFROM zerver_message')

    def test_add_term_using_in_operator_and_not_defined_operand(self) -> None:
        term = dict(operator='in', operand='not_defined')
        self.assertRaises(BadNarrowOperator, self._build_query, term)

    def test_add_term_using_near_operator(self) -> None:
        term = dict(operator='near', operand='operand')
        query = self._build_query(term)
        self.assertEqual(get_sqlalchemy_sql(query), 'SELECT id \nFROM zerver_message')

    def _do_add_term_test(self, term: Dict[str, Any], where_clause: str,
                          params: Optional[Dict[str, Any]]=None) -> None:
        query = self._build_query(term)
        if params is not None:
            actual_params = get_sqlalchemy_query_params(query)
            self.assertEqual(actual_params, params)
        self.assertIn(where_clause, get_sqlalchemy_sql(query))

    def _build_query(self, term: Dict[str, Any]) -> Query:
        return self.builder.add_term(self.raw_query, term)

class NarrowLibraryTest(ZulipTestCase):
    def test_build_narrow_filter(self) -> None:
        fixtures_path = os.path.join(os.path.dirname(__file__),
                                     'fixtures/narrow.json')
        with open(fixtures_path) as f:
            scenarios = ujson.load(f)
        self.assertTrue(len(scenarios) == 9)
        for scenario in scenarios:
            narrow = scenario['narrow']
            accept_events = scenario['accept_events']
            reject_events = scenario['reject_events']
            narrow_filter = build_narrow_filter(narrow)
            for e in accept_events:
                self.assertTrue(narrow_filter(e))
            for e in reject_events:
                self.assertFalse(narrow_filter(e))

    def test_build_narrow_filter_invalid(self) -> None:
        with self.assertRaises(JsonableError):
            build_narrow_filter(["invalid_operator", "operand"])

    def test_is_web_public_compatible(self) -> None:
        self.assertTrue(is_web_public_compatible([]))
        self.assertTrue(is_web_public_compatible([{"operator": "has",
                                                   "operand": "attachment"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "has",
                                                   "operand": "image"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "search",
                                                   "operand": "magic"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "near",
                                                   "operand": "15"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "id",
                                                   "operand": "15"},
                                                  {"operator": "has",
                                                   "operand": "attachment"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "sender",
                                                   "operand": "hamlet@zulip.com"}]))
        self.assertFalse(is_web_public_compatible([{"operator": "pm-with",
                                                    "operand": "hamlet@zulip.com"}]))
        self.assertFalse(is_web_public_compatible([{"operator": "group-pm-with",
                                                    "operand": "hamlet@zulip.com"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "stream",
                                                   "operand": "Denmark"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "stream",
                                                   "operand": "Denmark"},
                                                  {"operator": "topic",
                                                   "operand": "logic"}]))
        self.assertFalse(is_web_public_compatible([{"operator": "is",
                                                    "operand": "starred"}]))
        self.assertFalse(is_web_public_compatible([{"operator": "is",
                                                    "operand": "private"}]))
        self.assertTrue(is_web_public_compatible([{"operator": "streams",
                                                   "operand": "public"}]))
        # Malformed input not allowed
        self.assertFalse(is_web_public_compatible([{"operator": "has"}]))

class IncludeHistoryTest(ZulipTestCase):
    def test_ok_to_include_history(self) -> None:
        user_profile = self.example_user("hamlet")
        self.make_stream('public_stream', realm=user_profile.realm)

        # Negated stream searches should not include history.
        narrow = [
            dict(operator='stream', operand='public_stream', negated=True),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # streams:public searches should include history for non-guest members.
        narrow = [
            dict(operator='streams', operand='public'),
        ]
        self.assertTrue(ok_to_include_history(narrow, user_profile))

        # Negated -streams:public searches should not include history.
        narrow = [
            dict(operator='streams', operand='public', negated=True),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # Definitely forbid seeing history on private streams.
        self.make_stream('private_stream', realm=user_profile.realm, invite_only=True)
        subscribed_user_profile = self.example_user("cordelia")
        self.subscribe(subscribed_user_profile, 'private_stream')
        narrow = [
            dict(operator='stream', operand='private_stream'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # Verify that with stream.history_public_to_subscribers, subscribed
        # users can access history.
        self.make_stream('private_stream_2', realm=user_profile.realm,
                         invite_only=True, history_public_to_subscribers=True)
        subscribed_user_profile = self.example_user("cordelia")
        self.subscribe(subscribed_user_profile, 'private_stream_2')
        narrow = [
            dict(operator='stream', operand='private_stream_2'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))
        self.assertTrue(ok_to_include_history(narrow, subscribed_user_profile))

        # History doesn't apply to PMs.
        narrow = [
            dict(operator='is', operand='private'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # History doesn't apply to unread messages.
        narrow = [
            dict(operator='is', operand='unread'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # If we are looking for something like starred messages, there is
        # no point in searching historical messages.
        narrow = [
            dict(operator='stream', operand='public_stream'),
            dict(operator='is', operand='starred'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # No point in searching history for is operator even if included with
        # streams:public
        narrow = [
            dict(operator='streams', operand='public'),
            dict(operator='is', operand='mentioned'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))
        narrow = [
            dict(operator='streams', operand='public'),
            dict(operator='is', operand='unread'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))
        narrow = [
            dict(operator='streams', operand='public'),
            dict(operator='is', operand='alerted'),
        ]
        self.assertFalse(ok_to_include_history(narrow, user_profile))

        # simple True case
        narrow = [
            dict(operator='stream', operand='public_stream'),
        ]
        self.assertTrue(ok_to_include_history(narrow, user_profile))

        narrow = [
            dict(operator='stream', operand='public_stream'),
            dict(operator='topic', operand='whatever'),
            dict(operator='search', operand='needle in haystack'),
        ]
        self.assertTrue(ok_to_include_history(narrow, user_profile))

        # Tests for guest user
        guest_user_profile = self.example_user("polonius")
        # Using 'Cordelia' to compare between a guest and a normal user
        subscribed_user_profile = self.example_user("cordelia")

        # streams:public searches should not include history for guest members.
        narrow = [
            dict(operator='streams', operand='public'),
        ]
        self.assertFalse(ok_to_include_history(narrow, guest_user_profile))

        # Guest user can't access public stream
        self.subscribe(subscribed_user_profile, 'public_stream_2')
        narrow = [
            dict(operator='stream', operand='public_stream_2'),
        ]
        self.assertFalse(ok_to_include_history(narrow, guest_user_profile))
        self.assertTrue(ok_to_include_history(narrow, subscribed_user_profile))

        # Definitely, a guest user can't access the unsubscribed private stream
        self.subscribe(subscribed_user_profile, 'private_stream_3')
        narrow = [
            dict(operator='stream', operand='private_stream_3'),
        ]
        self.assertFalse(ok_to_include_history(narrow, guest_user_profile))
        self.assertTrue(ok_to_include_history(narrow, subscribed_user_profile))

        # Guest user can access (history of) subscribed private streams
        self.subscribe(guest_user_profile, 'private_stream_4')
        self.subscribe(subscribed_user_profile, 'private_stream_4')
        narrow = [
            dict(operator='stream', operand='private_stream_4'),
        ]
        self.assertTrue(ok_to_include_history(narrow, guest_user_profile))
        self.assertTrue(ok_to_include_history(narrow, subscribed_user_profile))

class PostProcessTest(ZulipTestCase):
    def test_basics(self) -> None:
        def verify(in_ids: List[int],
                   num_before: int,
                   num_after: int,
                   first_visible_message_id: int,
                   anchor: int,
                   anchored_to_left: bool,
                   anchored_to_right: bool,
                   out_ids: List[int],
                   found_anchor: bool,
                   found_oldest: bool,
                   found_newest: bool,
                   history_limited: bool) -> None:
            in_rows = [[row_id] for row_id in in_ids]
            out_rows = [[row_id] for row_id in out_ids]

            info = post_process_limited_query(
                rows=in_rows,
                num_before=num_before,
                num_after=num_after,
                anchor=anchor,
                anchored_to_left=anchored_to_left,
                anchored_to_right=anchored_to_right,
                first_visible_message_id=first_visible_message_id,
            )

            self.assertEqual(info['rows'], out_rows)
            self.assertEqual(info['found_anchor'], found_anchor)
            self.assertEqual(info['found_newest'], found_newest)
            self.assertEqual(info['found_oldest'], found_oldest)
            self.assertEqual(info['history_limited'], history_limited)

        # typical 2-sided query, with a bunch of tests for different
        # values of first_visible_message_id.
        anchor = 10
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[8, 9, 10, 11, 12],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=8,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[8, 9, 10, 11, 12],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=9,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[9, 10, 11, 12],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=10,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[10, 11, 12],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=11,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[11, 12],
            found_anchor=False, found_oldest=True,
            found_newest=False, history_limited=True,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=12,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[12],
            found_anchor=False, found_oldest=True,
            found_newest=True, history_limited=True,
        )
        verify(
            in_ids=[8, 9, anchor, 11, 12],
            num_before=2, num_after=2,
            first_visible_message_id=13,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[],
            found_anchor=False, found_oldest=True,
            found_newest=True, history_limited=True,
        )

        # typical 2-sided query missing anchor and grabbing an extra row
        anchor = 10
        verify(
            in_ids=[7, 9, 11, 13, 15],
            num_before=2, num_after=2,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            first_visible_message_id=0,
            out_ids=[7, 9, 11, 13],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[7, 9, 11, 13, 15],
            num_before=2, num_after=2,
            first_visible_message_id=10,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[11, 13],
            found_anchor=False, found_oldest=True,
            found_newest=False, history_limited=True,
        )
        verify(
            in_ids=[7, 9, 11, 13, 15],
            num_before=2, num_after=2,
            first_visible_message_id=9,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[9, 11, 13],
            found_anchor=False, found_oldest=True,
            found_newest=False, history_limited=True,
        )

        # 2-sided query with old anchor
        anchor = 100
        verify(
            in_ids=[50, anchor, 150, 200],
            num_before=2, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[50, 100, 150, 200],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[50, anchor, 150, 200],
            num_before=2, num_after=2,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[100, 150, 200],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )

        # 2-sided query with new anchor
        anchor = 900
        verify(
            in_ids=[700, 800, anchor, 1000],
            num_before=2, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[700, 800, 900, 1000],
            found_anchor=True, found_oldest=False,
            found_newest=True, history_limited=False,
        )
        verify(
            in_ids=[700, 800, anchor, 1000],
            num_before=2, num_after=2,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[900, 1000],
            found_anchor=True, found_oldest=True,
            found_newest=True, history_limited=True,
        )

        # left-sided query with old anchor
        anchor = 100
        verify(
            in_ids=[50, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[50, 100],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[50, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[100],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )

        # left-sided query with new anchor
        anchor = 900
        verify(
            in_ids=[700, 800, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[700, 800, 900],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[700, 800, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[900],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )

        # left-sided query with new anchor and extra row
        anchor = 900
        verify(
            in_ids=[600, 700, 800, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[700, 800, 900],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[600, 700, 800, anchor],
            num_before=2, num_after=0,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[900],
            found_anchor=True, found_oldest=True,
            found_newest=False, history_limited=True,
        )

        # left-sided query anchored to the right
        anchor = None
        verify(
            in_ids=[900, 1000],
            num_before=2, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=True,
            out_ids=[900, 1000],
            found_anchor=False, found_oldest=False,
            found_newest=True, history_limited=False,
        )
        verify(
            in_ids=[900, 1000],
            num_before=2, num_after=0,
            first_visible_message_id=1000,
            anchor=anchor, anchored_to_left=False, anchored_to_right=True,
            out_ids=[1000],
            found_anchor=False, found_oldest=True,
            found_newest=True, history_limited=True,
        )
        verify(
            in_ids=[900, 1000],
            num_before=2, num_after=0,
            first_visible_message_id=1100,
            anchor=anchor, anchored_to_left=False, anchored_to_right=True,
            out_ids=[],
            found_anchor=False, found_oldest=True,
            found_newest=True, history_limited=True,
        )

        # right-sided query with old anchor
        anchor = 100
        verify(
            in_ids=[anchor, 200, 300, 400],
            num_before=0, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[100, 200, 300],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[anchor, 200, 300, 400],
            num_before=0, num_after=2,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[100, 200, 300],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[anchor, 200, 300, 400],
            num_before=0, num_after=2,
            first_visible_message_id=300,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[300, 400],
            found_anchor=False, found_oldest=False,
            # BUG: history_limited should be False here.
            found_newest=False, history_limited=False,
        )

        # right-sided query with new anchor
        anchor = 900
        verify(
            in_ids=[anchor, 1000],
            num_before=0, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[900, 1000],
            found_anchor=True, found_oldest=False,
            found_newest=True, history_limited=False,
        )
        verify(
            in_ids=[anchor, 1000],
            num_before=0, num_after=2,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[900, 1000],
            found_anchor=True, found_oldest=False,
            found_newest=True, history_limited=False,
        )

        # right-sided query with non-matching anchor
        anchor = 903
        verify(
            in_ids=[1000, 1100, 1200],
            num_before=0, num_after=2,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1000, 1100],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[1000, 1100, 1200],
            num_before=0, num_after=2,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1000, 1100],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[1000, 1100, 1200],
            num_before=0, num_after=2,
            first_visible_message_id=1000,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1000, 1100],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[1000, 1100, 1200],
            num_before=0, num_after=2,
            first_visible_message_id=1100,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1100, 1200],
            found_anchor=False, found_oldest=False,
            # BUG: history_limited should be False here.
            found_newest=False, history_limited=False,
        )

        # targeted query that finds row
        anchor = 1000
        verify(
            in_ids=[1000],
            num_before=0, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1000],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[1000],
            num_before=0, num_after=0,
            first_visible_message_id=anchor,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[1000],
            found_anchor=True, found_oldest=False,
            found_newest=False, history_limited=False,
        )
        verify(
            in_ids=[1000],
            num_before=0, num_after=0,
            first_visible_message_id=1100,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )

        # targeted query that finds nothing
        anchor = 903
        verify(
            in_ids=[],
            num_before=0, num_after=0,
            first_visible_message_id=0,
            anchor=anchor, anchored_to_left=False, anchored_to_right=False,
            out_ids=[],
            found_anchor=False, found_oldest=False,
            found_newest=False, history_limited=False,
        )

class GetOldMessagesTest(ZulipTestCase):

    def get_and_check_messages(self,
                               modified_params: Dict[str, Union[str, int]],
                               **kwargs: Any) -> Dict[str, Any]:
        post_params: Dict[str, Union[str, int]] = {"anchor": 1, "num_before": 1, "num_after": 1}
        post_params.update(modified_params)
        payload = self.client_get("/json/messages", dict(post_params),
                                  **kwargs)
        self.assert_json_success(payload)
        self.assertEqual(set(payload["Cache-Control"].split(", ")),
                         {"must-revalidate", "no-store", "no-cache", "max-age=0"})

        result = ujson.loads(payload.content)

        self.assertIn("messages", result)
        self.assertIsInstance(result["messages"], list)
        for message in result["messages"]:
            for field in ("content", "content_type", "display_recipient",
                          "avatar_url", "recipient_id", "sender_full_name",
                          "sender_short_name", "timestamp", "reactions"):
                self.assertIn(field, message)
        return result

    def message_visibility_test(self, narrow: List[Dict[str, str]],
                                message_ids: List[int], pivot_index: int) -> None:
        num_before = len(message_ids)

        post_params = dict(narrow=ujson.dumps(narrow), num_before=num_before,
                           num_after=0, anchor=LARGER_THAN_MAX_MESSAGE_ID)
        payload = self.client_get("/json/messages", dict(post_params))
        self.assert_json_success(payload)
        result = ujson.loads(payload.content)

        self.assertEqual(len(result["messages"]), len(message_ids))
        for message in result["messages"]:
            assert(message["id"] in message_ids)

        post_params.update({"num_before": len(message_ids[pivot_index:])})

        with first_visible_id_as(message_ids[pivot_index]):
            payload = self.client_get("/json/messages", dict(post_params))

        self.assert_json_success(payload)
        result = ujson.loads(payload.content)

        self.assertEqual(len(result["messages"]), len(message_ids[pivot_index:]))
        for message in result["messages"]:
            assert(message["id"] in message_ids)

    def get_query_ids(self) -> Dict[str, Union[int, str]]:
        hamlet_user = self.example_user('hamlet')
        othello_user = self.example_user('othello')

        query_ids: Dict[str, Union[int, str]] = {}

        scotland_stream = get_stream('Scotland', hamlet_user.realm)
        query_ids['scotland_recipient'] = scotland_stream.recipient_id
        query_ids['hamlet_id'] = hamlet_user.id
        query_ids['othello_id'] = othello_user.id
        query_ids['hamlet_recipient'] = hamlet_user.recipient_id
        query_ids['othello_recipient'] = othello_user.recipient_id
        recipients = Recipient.objects.filter(
            type=Recipient.STREAM,
            type_id__in=Stream.objects.filter(realm=hamlet_user.realm, invite_only=False),
        ).values('id').order_by('id')
        query_ids['public_streams_recipents'] = ", ".join(str(r['id']) for r in recipients)
        return query_ids

    def test_content_types(self) -> None:
        """
        Test old `/json/messages` returns reactions.
        """
        self.login('hamlet')

        def get_content_type(apply_markdown: bool) -> str:
            req: Dict[str, Any] = dict(
                apply_markdown=ujson.dumps(apply_markdown),
            )
            result = self.get_and_check_messages(req)
            message = result['messages'][0]
            return message['content_type']

        self.assertEqual(
            get_content_type(apply_markdown=False),
            'text/x-markdown',
        )

        self.assertEqual(
            get_content_type(apply_markdown=True),
            'text/html',
        )

    def test_successful_get_messages_reaction(self) -> None:
        """
        Test old `/json/messages` returns reactions.
        """
        self.login('hamlet')
        messages = self.get_and_check_messages(dict())
        message_id = messages['messages'][0]['id']

        self.login('othello')
        reaction_name = 'thumbs_up'
        reaction_info = {
            'emoji_name': reaction_name,
        }

        url = f'/json/messages/{message_id}/reactions'
        payload = self.client_post(url, reaction_info)
        self.assert_json_success(payload)

        self.login('hamlet')
        messages = self.get_and_check_messages({})
        message_to_assert = None
        for message in messages['messages']:
            if message['id'] == message_id:
                message_to_assert = message
                break
        assert(message_to_assert is not None)
        self.assertEqual(len(message_to_assert['reactions']), 1)
        self.assertEqual(message_to_assert['reactions'][0]['emoji_name'],
                         reaction_name)

    def test_successful_get_messages(self) -> None:
        """
        A call to GET /json/messages with valid parameters returns a list of
        messages.
        """
        self.login('hamlet')
        self.get_and_check_messages(dict())

        othello_email = self.example_user('othello').email

        # We have to support the legacy tuple style while there are old
        # clients around, which might include third party home-grown bots.
        self.get_and_check_messages(
            dict(
                narrow=ujson.dumps(
                    [['pm-with', othello_email]],
                ),
            ),
        )

        self.get_and_check_messages(
            dict(
                narrow=ujson.dumps(
                    [dict(operator='pm-with', operand=othello_email)],
                ),
            ),
        )

    def test_client_avatar(self) -> None:
        """
        The client_gravatar flag determines whether we send avatar_url.
        """
        hamlet = self.example_user('hamlet')
        self.login_user(hamlet)

        do_set_realm_property(hamlet.realm, "email_address_visibility",
                              Realm.EMAIL_ADDRESS_VISIBILITY_EVERYONE)

        self.send_personal_message(hamlet, self.example_user("iago"))

        result = self.get_and_check_messages({})
        message = result['messages'][0]
        self.assertIn('gravatar.com', message['avatar_url'])

        result = self.get_and_check_messages(dict(client_gravatar=ujson.dumps(True)))
        message = result['messages'][0]
        self.assertEqual(message['avatar_url'], None)

        # Now verify client_gravatar doesn't run with EMAIL_ADDRESS_VISIBILITY_ADMINS
        do_set_realm_property(hamlet.realm, "email_address_visibility",
                              Realm.EMAIL_ADDRESS_VISIBILITY_ADMINS)
        result = self.get_and_check_messages(dict(client_gravatar=ujson.dumps(True)))
        message = result['messages'][0]
        self.assertIn('gravatar.com', message['avatar_url'])

    def test_get_messages_with_narrow_pm_with(self) -> None:
        """
        A request for old messages with a narrow by pm-with only returns
        conversations with that user.
        """
        me = self.example_user('hamlet')

        def dr_emails(dr: DisplayRecipientT) -> str:
            assert isinstance(dr, list)
            return ','.join(sorted(set([r['email'] for r in dr] + [me.email])))

        def dr_ids(dr: DisplayRecipientT) -> List[int]:
            assert isinstance(dr, list)
            return list(sorted(set([r['id'] for r in dr] + [self.example_user('hamlet').id])))

        self.send_personal_message(me, self.example_user("iago"))

        self.send_huddle_message(
            me,
            [self.example_user("iago"), self.example_user("cordelia")],
        )

        # Send a 1:1 and group PM containing Aaron.
        # Then deactivate aaron to test pm-with narrow includes messages
        # from deactivated users also.
        self.send_personal_message(me, self.example_user("aaron"))
        self.send_huddle_message(
            me,
            [self.example_user("iago"), self.example_user("aaron")],
        )
        aaron = self.example_user("aaron")
        do_deactivate_user(aaron)
        self.assertFalse(aaron.is_active)

        personals = [m for m in get_user_messages(self.example_user('hamlet'))
                     if not m.is_stream_message()]
        for personal in personals:
            emails = dr_emails(get_display_recipient(personal.recipient))
            self.login_user(me)
            narrow: List[Dict[str, Any]] = [dict(operator='pm-with', operand=emails)]
            result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow)))

            for message in result["messages"]:
                self.assertEqual(dr_emails(message['display_recipient']), emails)

            # check passing id is conistent with passing emails as operand
            ids = dr_ids(get_display_recipient(personal.recipient))
            narrow = [dict(operator='pm-with', operand=ids)]
            result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow)))

            for message in result["messages"]:
                self.assertEqual(dr_emails(message['display_recipient']), emails)

    def test_get_visible_messages_with_narrow_pm_with(self) -> None:
        me = self.example_user('hamlet')
        self.login_user(me)
        self.subscribe(self.example_user("hamlet"), 'Scotland')

        message_ids = []
        for i in range(5):
            message_ids.append(self.send_personal_message(me, self.example_user("iago")))

        narrow = [dict(operator='pm-with', operand=self.example_user("iago").email)]
        self.message_visibility_test(narrow, message_ids, 2)

    def test_get_messages_with_narrow_group_pm_with(self) -> None:
        """
        A request for old messages with a narrow by group-pm-with only returns
        group-private conversations with that user.
        """
        me = self.example_user("hamlet")

        iago = self.example_user("iago")
        cordelia = self.example_user("cordelia")
        othello = self.example_user("othello")

        matching_message_ids = []

        matching_message_ids.append(
            self.send_huddle_message(
                me,
                [iago, cordelia, othello],
            ),
        )

        matching_message_ids.append(
            self.send_huddle_message(
                me,
                [cordelia, othello],
            ),
        )

        non_matching_message_ids = []

        non_matching_message_ids.append(
            self.send_personal_message(me, cordelia),
        )

        non_matching_message_ids.append(
            self.send_huddle_message(
                me,
                [iago, othello],
            ),
        )

        non_matching_message_ids.append(
            self.send_huddle_message(
                self.example_user("cordelia"),
                [iago, othello],
            ),
        )

        self.login_user(me)
        test_operands = [cordelia.email, cordelia.id]
        for operand in test_operands:
            narrow = [dict(operator='group-pm-with', operand=operand)]
            result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow)))
            for message in result["messages"]:
                self.assertIn(message["id"], matching_message_ids)
                self.assertNotIn(message["id"], non_matching_message_ids)

    def test_get_visible_messages_with_narrow_group_pm_with(self) -> None:
        me = self.example_user('hamlet')
        self.login_user(me)

        iago = self.example_user("iago")
        cordelia = self.example_user("cordelia")
        othello = self.example_user("othello")

        message_ids = []
        message_ids.append(
            self.send_huddle_message(
                me,
                [iago, cordelia, othello],
            ),
        )
        message_ids.append(
            self.send_huddle_message(
                me,
                [cordelia, othello],
            ),
        )
        message_ids.append(
            self.send_huddle_message(
                me,
                [cordelia, iago],
            ),
        )

        narrow = [dict(operator='group-pm-with', operand=cordelia.email)]
        self.message_visibility_test(narrow, message_ids, 1)

    def test_include_history(self) -> None:
        hamlet = self.example_user('hamlet')
        cordelia = self.example_user('cordelia')

        stream_name = 'test stream'
        self.subscribe(cordelia, stream_name)

        old_message_id = self.send_stream_message(cordelia, stream_name, content='foo')

        self.subscribe(hamlet, stream_name)

        content = 'hello @**King Hamlet**'
        new_message_id = self.send_stream_message(cordelia, stream_name, content=content)

        self.login_user(hamlet)
        narrow = [
            dict(operator='stream', operand=stream_name),
        ]

        req = dict(
            narrow=ujson.dumps(narrow),
            anchor=LARGER_THAN_MAX_MESSAGE_ID,
            num_before=100,
            num_after=100,
        )

        payload = self.client_get('/json/messages', req)
        self.assert_json_success(payload)
        result = ujson.loads(payload.content)
        messages = result['messages']
        self.assertEqual(len(messages), 2)

        for message in messages:
            if message['id'] == old_message_id:
                old_message = message
            elif message['id'] == new_message_id:
                new_message = message

        self.assertEqual(old_message['flags'], ['read', 'historical'])
        self.assertEqual(new_message['flags'], ['mentioned'])

    def test_get_messages_with_narrow_stream(self) -> None:
        """
        A request for old messages with a narrow by stream only returns
        messages for that stream.
        """
        self.login('hamlet')
        # We need to subscribe to a stream and then send a message to
        # it to ensure that we actually have a stream message in this
        # narrow view.
        self.subscribe(self.example_user("hamlet"), 'Scotland')
        self.send_stream_message(self.example_user("hamlet"), "Scotland")
        messages = get_user_messages(self.example_user('hamlet'))
        stream_messages = [msg for msg in messages if msg.is_stream_message()]
        stream_name = get_display_recipient(stream_messages[0].recipient)
        assert isinstance(stream_name, str)
        stream_id = get_stream(stream_name, stream_messages[0].get_realm()).id
        stream_recipient_id = stream_messages[0].recipient.id

        for operand in [stream_name, stream_id]:
            narrow = [dict(operator='stream', operand=operand)]
            result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow)))

            for message in result["messages"]:
                self.assertEqual(message["type"], "stream")
                self.assertEqual(message["recipient_id"], stream_recipient_id)

    def test_get_visible_messages_with_narrow_stream(self) -> None:
        self.login('hamlet')
        self.subscribe(self.example_user("hamlet"), 'Scotland')

        message_ids = []
        for i in range(5):
            message_ids.append(self.send_stream_message(self.example_user("iago"), "Scotland"))

        narrow = [dict(operator='stream', operand="Scotland")]
        self.message_visibility_test(narrow, message_ids, 2)

    def test_get_messages_with_narrow_stream_mit_unicode_regex(self) -> None:
        """
        A request for old messages for a user in the mit.edu relam with unicode
        stream name should be correctly escaped in the database query.
        """
        user = self.mit_user('starnine')
        self.login_user(user)
        # We need to susbcribe to a stream and then send a message to
        # it to ensure that we actually have a stream message in this
        # narrow view.
        lambda_stream_name = "\u03bb-stream"
        stream = self.subscribe(user, lambda_stream_name)
        self.assertTrue(stream.is_in_zephyr_realm)

        lambda_stream_d_name = "\u03bb-stream.d"
        self.subscribe(user, lambda_stream_d_name)

        self.send_stream_message(user, "\u03bb-stream")
        self.send_stream_message(user, "\u03bb-stream.d")

        narrow = [dict(operator='stream', operand='\u03bb-stream')]
        result = self.get_and_check_messages(dict(num_after=2,
                                                  narrow=ujson.dumps(narrow)),
                                             subdomain="zephyr")

        messages = get_user_messages(self.mit_user("starnine"))
        stream_messages = [msg for msg in messages if msg.is_stream_message()]

        self.assertEqual(len(result["messages"]), 2)
        for i, message in enumerate(result["messages"]):
            self.assertEqual(message["type"], "stream")
            stream_id = stream_messages[i].recipient.id
            self.assertEqual(message["recipient_id"], stream_id)

    def test_get_messages_with_narrow_topic_mit_unicode_regex(self) -> None:
        """
        A request for old messages for a user in the mit.edu realm with unicode
        topic name should be correctly escaped in the database query.
        """
        mit_user_profile = self.mit_user("starnine")
        self.login_user(mit_user_profile)
        # We need to susbcribe to a stream and then send a message to
        # it to ensure that we actually have a stream message in this
        # narrow view.
        self.subscribe(mit_user_profile, "Scotland")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="\u03bb-topic")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="\u03bb-topic.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="\u03bb-topic.d.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="\u03bb-topic.d.d.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="\u03bb-topic.d.d.d.d")

        narrow = [dict(operator='topic', operand='\u03bb-topic')]
        result = self.get_and_check_messages(
            dict(num_after=100, narrow=ujson.dumps(narrow)),
            subdomain="zephyr")

        messages = get_user_messages(mit_user_profile)
        stream_messages = [msg for msg in messages if msg.is_stream_message()]
        self.assertEqual(len(result["messages"]), 5)
        for i, message in enumerate(result["messages"]):
            self.assertEqual(message["type"], "stream")
            stream_id = stream_messages[i].recipient.id
            self.assertEqual(message["recipient_id"], stream_id)

    def test_get_messages_with_narrow_topic_mit_personal(self) -> None:
        """
        We handle .d grouping for MIT realm personal messages correctly.
        """
        mit_user_profile = self.mit_user("starnine")

        # We need to susbcribe to a stream and then send a message to
        # it to ensure that we actually have a stream message in this
        # narrow view.
        self.login_user(mit_user_profile)
        self.subscribe(mit_user_profile, "Scotland")

        self.send_stream_message(mit_user_profile, "Scotland", topic_name=".d.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="PERSONAL")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name='(instance "").d')
        self.send_stream_message(mit_user_profile, "Scotland", topic_name=".d.d.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name="personal.d")
        self.send_stream_message(mit_user_profile, "Scotland", topic_name='(instance "")')
        self.send_stream_message(mit_user_profile, "Scotland", topic_name=".d.d.d.d")

        narrow = [dict(operator='topic', operand='personal.d.d')]
        result = self.get_and_check_messages(
            dict(num_before=50,
                 num_after=50,
                 narrow=ujson.dumps(narrow)),
            subdomain="zephyr")

        messages = get_user_messages(mit_user_profile)
        stream_messages = [msg for msg in messages if msg.is_stream_message()]
        self.assertEqual(len(result["messages"]), 7)
        for i, message in enumerate(result["messages"]):
            self.assertEqual(message["type"], "stream")
            stream_id = stream_messages[i].recipient.id
            self.assertEqual(message["recipient_id"], stream_id)

    def test_get_messages_with_narrow_sender(self) -> None:
        """
        A request for old messages with a narrow by sender only returns
        messages sent by that person.
        """
        self.login('hamlet')

        hamlet = self.example_user('hamlet')
        othello = self.example_user('othello')
        iago = self.example_user('iago')

        # We need to send a message here to ensure that we actually
        # have a stream message in this narrow view.
        self.send_stream_message(hamlet, "Scotland")
        self.send_stream_message(othello, "Scotland")
        self.send_personal_message(othello, hamlet)
        self.send_stream_message(iago, "Scotland")

        test_operands = [othello.email, othello.id]
        for operand in test_operands:
            narrow = [dict(operator='sender', operand=operand)]
            result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow)))

            for message in result["messages"]:
                self.assertEqual(message["sender_id"], othello.id)

    def _update_tsvector_index(self) -> None:
        # We use brute force here and update our text search index
        # for the entire zerver_message table (which is small in test
        # mode).  In production there is an async process which keeps
        # the search index up to date.
        with connection.cursor() as cursor:
            cursor.execute("""
            UPDATE zerver_message SET
            search_tsvector = to_tsvector('zulip.english_us_search',
            subject || rendered_content)
            """)

    @override_settings(USING_PGROONGA=False)
    def test_messages_in_narrow(self) -> None:
        user = self.example_user("cordelia")
        self.login_user(user)

        def send(content: str) -> int:
            msg_id = self.send_stream_message(
                sender=user,
                stream_name="Verona",
                content=content,
            )
            return msg_id

        good_id = send('KEYWORDMATCH and should work')
        bad_id = send('no match')
        msg_ids = [good_id, bad_id]
        send('KEYWORDMATCH but not in msg_ids')

        self._update_tsvector_index()

        narrow = [
            dict(operator='search', operand='KEYWORDMATCH'),
        ]

        raw_params = dict(msg_ids=msg_ids, narrow=narrow)
        params = {k: ujson.dumps(v) for k, v in raw_params.items()}
        result = self.client_get('/json/messages/matches_narrow', params)
        self.assert_json_success(result)
        messages = result.json()['messages']
        self.assertEqual(len(list(messages.keys())), 1)
        message = messages[str(good_id)]
        self.assertEqual(message['match_content'],
                         '<p><span class="highlight">KEYWORDMATCH</span> and should work</p>')

    @override_settings(USING_PGROONGA=False)
    def test_get_messages_with_search(self) -> None:
        self.login('cordelia')

        messages_to_search = [
            ('breakfast', 'there are muffins in the conference room'),
            ('lunch plans', 'I am hungry!'),
            ('meetings', 'discuss lunch after lunch'),
            ('meetings', 'please bring your laptops to take notes'),
            ('dinner', 'Anybody staying late tonight?'),
            ('urltest', 'https://google.com'),
            ('日本', 'こんに ちは 。 今日は いい 天気ですね。'),
            ('日本', '今朝はごはんを食べました。'),
            ('日本', '昨日、日本 のお菓子を送りました。'),
            ('english', 'I want to go to 日本!'),
        ]

        next_message_id = self.get_last_message().id + 1

        cordelia = self.example_user('cordelia')

        for topic, content in messages_to_search:
            self.send_stream_message(
                sender=cordelia,
                stream_name="Verona",
                content=content,
                topic_name=topic,
            )

        self._update_tsvector_index()

        narrow = [
            dict(operator='sender', operand=cordelia.email),
            dict(operator='search', operand='lunch'),
        ]
        result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_before=0,
            num_after=10,
        ))
        self.assertEqual(len(result['messages']), 2)
        messages = result['messages']

        narrow = [dict(operator='search', operand='https://google.com')]
        link_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_before=0,
            num_after=10,
        ))
        self.assertEqual(len(link_search_result['messages']), 1)
        self.assertEqual(link_search_result['messages'][0]['match_content'],
                         '<p><a href="https://google.com">https://<span class="highlight">google.com</span></a></p>')

        (meeting_message,) = [
            m for m in messages
            if m[TOPIC_NAME] == 'meetings'
        ]
        self.assertEqual(
            meeting_message[MATCH_TOPIC],
            'meetings')
        self.assertEqual(
            meeting_message['match_content'],
            '<p>discuss <span class="highlight">lunch</span> after ' +
            '<span class="highlight">lunch</span></p>')

        (lunch_message,) = [
            m for m in messages
            if m[TOPIC_NAME] == 'lunch plans'
        ]
        self.assertEqual(
            lunch_message[MATCH_TOPIC],
            '<span class="highlight">lunch</span> plans')
        self.assertEqual(
            lunch_message['match_content'],
            '<p>I am hungry!</p>')

        # Should not crash when multiple search operands are present
        multi_search_narrow = [
            dict(operator='search', operand='discuss'),
            dict(operator='search', operand='after'),
        ]
        multi_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(multi_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(multi_search_result['messages']), 1)
        self.assertEqual(multi_search_result['messages'][0]['match_content'], '<p><span class="highlight">discuss</span> lunch <span class="highlight">after</span> lunch</p>')

        # Test searching in messages with unicode characters
        narrow = [
            dict(operator='search', operand='日本'),
        ]
        result = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(result['messages']), 4)
        messages = result['messages']

        japanese_message = [
            m for m in messages
            if m[TOPIC_NAME] == '日本'][-1]
        self.assertEqual(
            japanese_message[MATCH_TOPIC],
            '<span class="highlight">日本</span>')
        self.assertEqual(
            japanese_message['match_content'],
            '<p>昨日、<span class="highlight">日本</span>' +
            ' のお菓子を送りました。</p>')

        (english_message,) = [
            m for m in messages
            if m[TOPIC_NAME] == 'english'
        ]
        self.assertEqual(
            english_message[MATCH_TOPIC],
            'english')
        self.assertIn(
            english_message['match_content'],
            '<p>I want to go to <span class="highlight">日本</span>!</p>')

        # Multiple search operands with unicode
        multi_search_narrow = [
            dict(operator='search', operand='ちは'),
            dict(operator='search', operand='今日は'),
        ]
        multi_search_result = self.get_and_check_messages(dict(
            narrow=ujson.dumps(multi_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(multi_search_result['messages']), 1)
        self.assertEqual(multi_search_result['messages'][0]['match_content'],
                         '<p>こんに <span class="highlight">ちは</span> 。 <span class="highlight">今日は</span> いい 天気ですね。</p>')

    @override_settings(USING_PGROONGA=False)
    def test_get_visible_messages_with_search(self) -> None:
        self.login('hamlet')
        self.subscribe(self.example_user("hamlet"), 'Scotland')

        messages_to_search = [
            ("Gryffindor", "Hogwart's house which values courage, bravery, nerve, and chivalry"),
            ("Hufflepuff", "Hogwart's house which values hard work, patience, justice, and loyalty."),
            ("Ravenclaw", "Hogwart's house which values intelligence, creativity, learning, and wit"),
            ("Slytherin", "Hogwart's house which  values ambition, cunning, leadership, and resourcefulness"),
        ]

        message_ids = []
        for topic, content in messages_to_search:
            message_ids.append(self.send_stream_message(self.example_user("iago"), "Scotland",
                                                        topic_name=topic, content=content))
        self._update_tsvector_index()
        narrow = [dict(operator='search', operand="Hogwart's")]
        self.message_visibility_test(narrow, message_ids, 2)

    @override_settings(USING_PGROONGA=False)
    def test_get_messages_with_search_not_subscribed(self) -> None:
        """Verify support for searching a stream you're not subscribed to"""
        self.subscribe(self.example_user("hamlet"), "newstream")
        self.send_stream_message(
            sender=self.example_user("hamlet"),
            stream_name="newstream",
            content="Public special content!",
            topic_name="new",
        )
        self._update_tsvector_index()

        self.login('cordelia')

        stream_search_narrow = [
            dict(operator='search', operand='special'),
            dict(operator='stream', operand='newstream'),
        ]
        stream_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(stream_search_narrow),
            anchor=0,
            num_after=10,
            num_before=10,
        ))
        self.assertEqual(len(stream_search_result['messages']), 1)
        self.assertEqual(stream_search_result['messages'][0]['match_content'],
                         '<p>Public <span class="highlight">special</span> content!</p>')

    @override_settings(USING_PGROONGA=True)
    def test_get_messages_with_search_pgroonga(self) -> None:
        self.login('cordelia')

        next_message_id = self.get_last_message().id + 1

        messages_to_search = [
            ('日本語', 'こんにちは。今日はいい天気ですね。'),
            ('日本語', '今朝はごはんを食べました。'),
            ('日本語', '昨日、日本のお菓子を送りました。'),
            ('english', 'I want to go to 日本!'),
            ('english', 'Can you speak https://en.wikipedia.org/wiki/Japanese?'),
            ('english', 'https://google.com'),
            ('bread & butter', 'chalk & cheese'),
        ]

        for topic, content in messages_to_search:
            self.send_stream_message(
                sender=self.example_user("cordelia"),
                stream_name="Verona",
                content=content,
                topic_name=topic,
            )

        # We use brute force here and update our text search index
        # for the entire zerver_message table (which is small in test
        # mode).  In production there is an async process which keeps
        # the search index up to date.
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE zerver_message SET
                search_pgroonga = escape_html(subject) || ' ' || rendered_content
                """)

        narrow = [
            dict(operator='search', operand='日本'),
        ]
        result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(result['messages']), 4)
        messages = result['messages']

        japanese_message = [m for m in messages if m[TOPIC_NAME] == '日本語'][-1]
        self.assertEqual(
            japanese_message[MATCH_TOPIC],
            '<span class="highlight">日本</span>語')
        self.assertEqual(
            japanese_message['match_content'],
            '<p>昨日、<span class="highlight">日本</span>の' +
            'お菓子を送りました。</p>')

        english_message = [m for m in messages if m[TOPIC_NAME] == 'english'][0]
        self.assertEqual(
            english_message[MATCH_TOPIC],
            'english')
        self.assertIn(
            english_message['match_content'],
            # NOTE: The whitespace here is off due to a pgroonga bug.
            # This bug is a pgroonga regression and according to one of
            # the author, this should be fixed in its next release.
            ['<p>I want to go to <span class="highlight">日本</span>!</p>',  # This is correct.
             '<p>I want to go to<span class="highlight"> 日本</span>!</p>'])

        # Should not crash when multiple search operands are present
        multi_search_narrow = [
            dict(operator='search', operand='can'),
            dict(operator='search', operand='speak'),
            dict(operator='search', operand='wiki'),
        ]
        multi_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(multi_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(multi_search_result['messages']), 1)
        self.assertEqual(multi_search_result['messages'][0]['match_content'],
                         '<p><span class="highlight">Can</span> you <span class="highlight">speak</span> <a href="https://en.wikipedia.org/wiki/Japanese">https://en.<span class="highlight">wiki</span>pedia.org/<span class="highlight">wiki</span>/Japanese</a>?</p>')

        # Multiple search operands with unicode
        multi_search_narrow = [
            dict(operator='search', operand='朝は'),
            dict(operator='search', operand='べました'),
        ]
        multi_search_result = self.get_and_check_messages(dict(
            narrow=ujson.dumps(multi_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(multi_search_result['messages']), 1)
        self.assertEqual(multi_search_result['messages'][0]['match_content'],
                         '<p>今<span class="highlight">朝は</span>ごはんを食<span class="highlight">べました</span>。</p>')

        narrow = [dict(operator='search', operand='https://google.com')]
        link_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(link_search_result['messages']), 1)
        self.assertEqual(link_search_result['messages'][0]['match_content'],
                         '<p><a href="https://google.com"><span class="highlight">https://google.com</span></a></p>')

        # Search operands with HTML Special Characters
        special_search_narrow = [
            dict(operator='search', operand='butter'),
        ]
        special_search_result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(special_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(special_search_result['messages']), 1)
        self.assertEqual(special_search_result['messages'][0][MATCH_TOPIC],
                         'bread &amp; <span class="highlight">butter</span>')

        special_search_narrow = [
            dict(operator='search', operand='&'),
        ]
        special_search_result = self.get_and_check_messages(dict(
            narrow=ujson.dumps(special_search_narrow),
            anchor=next_message_id,
            num_after=10,
            num_before=0,
        ))
        self.assertEqual(len(special_search_result['messages']), 1)
        self.assertEqual(special_search_result['messages'][0][MATCH_TOPIC],
                         'bread <span class="highlight">&amp;</span> butter')
        self.assertEqual(special_search_result['messages'][0]['match_content'],
                         '<p>chalk <span class="highlight">&amp;</span> cheese</p>')

    def test_messages_in_narrow_for_non_search(self) -> None:
        user = self.example_user("cordelia")
        self.login_user(user)

        def send(content: str) -> int:
            msg_id = self.send_stream_message(
                sender=user,
                stream_name="Verona",
                topic_name='test_topic',
                content=content,
            )
            return msg_id

        good_id = send('http://foo.com')
        bad_id = send('no link here')
        msg_ids = [good_id, bad_id]
        send('http://bar.com but not in msg_ids')

        narrow = [
            dict(operator='has', operand='link'),
        ]

        raw_params = dict(msg_ids=msg_ids, narrow=narrow)
        params = {k: ujson.dumps(v) for k, v in raw_params.items()}
        result = self.client_get('/json/messages/matches_narrow', params)
        self.assert_json_success(result)
        messages = result.json()['messages']
        self.assertEqual(len(list(messages.keys())), 1)
        message = messages[str(good_id)]
        self.assertIn('a href=', message['match_content'])
        self.assertIn('http://foo.com', message['match_content'])
        self.assertEqual(message[MATCH_TOPIC], 'test_topic')

    def test_get_messages_with_only_searching_anchor(self) -> None:
        """
        Test that specifying an anchor but 0 for num_before and num_after
        returns at most 1 message.
        """
        self.login('cordelia')

        cordelia = self.example_user('cordelia')

        anchor = self.send_stream_message(cordelia, "Verona")

        narrow = [dict(operator='sender', operand=cordelia.email)]
        result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=anchor, num_before=0,
            num_after=0,
        ))
        self.assertEqual(len(result['messages']), 1)

        narrow = [dict(operator='is', operand='mentioned')]
        result = self.get_and_check_messages(dict(narrow=ujson.dumps(narrow),
                                                  anchor=anchor, num_before=0,
                                                  num_after=0))
        self.assertEqual(len(result['messages']), 0)

    def test_get_visible_messages_with_anchor(self) -> None:
        def messages_matches_ids(messages: List[Dict[str, Any]], message_ids: List[int]) -> None:
            self.assertEqual(len(messages), len(message_ids))
            for message in messages:
                assert(message["id"] in message_ids)

        self.login('hamlet')

        Message.objects.all().delete()

        message_ids = []
        for i in range(10):
            message_ids.append(self.send_stream_message(self.example_user("cordelia"), "Verona"))

        data = self.get_messages_response(anchor=message_ids[9], num_before=9, num_after=0)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids)

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[9], num_before=9, num_after=0)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], True)
        messages_matches_ids(messages, message_ids[5:])

        with first_visible_id_as(message_ids[2]):
            data = self.get_messages_response(anchor=message_ids[6], num_before=9, num_after=0)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], True)
        messages_matches_ids(messages, message_ids[2:7])

        with first_visible_id_as(message_ids[9] + 1):
            data = self.get_messages_response(anchor=message_ids[9], num_before=9, num_after=0)

        messages = data['messages']
        self.assert_length(messages, 0)
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], True)

        data = self.get_messages_response(anchor=message_ids[5], num_before=0, num_after=5)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids[5:])

        with first_visible_id_as(message_ids[7]):
            data = self.get_messages_response(anchor=message_ids[5], num_before=0, num_after=5)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids[7:])

        with first_visible_id_as(message_ids[2]):
            data = self.get_messages_response(anchor=message_ids[0], num_before=0, num_after=5)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids[2:7])

        with first_visible_id_as(message_ids[9] + 1):
            data = self.get_messages_response(anchor=message_ids[0], num_before=0, num_after=5)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)
        self.assert_length(messages, 0)

        # Verify that with anchor=0 we always get found_oldest=True
        with first_visible_id_as(0):
            data = self.get_messages_response(anchor=0, num_before=0, num_after=5)

        messages = data['messages']
        messages_matches_ids(messages, message_ids[0:5])
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)

        # Verify that with anchor=-1 we always get found_oldest=True
        # anchor=-1 is arguably invalid input, but it used to be supported
        with first_visible_id_as(0):
            data = self.get_messages_response(anchor=-1, num_before=0, num_after=5)

        messages = data['messages']
        messages_matches_ids(messages, message_ids[0:5])
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)

        # And anchor='first' does the same thing.
        with first_visible_id_as(0):
            data = self.get_messages_response(anchor='oldest', num_before=0, num_after=5)

        messages = data['messages']
        messages_matches_ids(messages, message_ids[0:5])
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)

        data = self.get_messages_response(anchor=message_ids[5], num_before=5, num_after=4)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids)

        data = self.get_messages_response(anchor=message_ids[5], num_before=10, num_after=10)
        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids)

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[5], num_before=5, num_after=4)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], True)
        messages_matches_ids(messages, message_ids[5:])

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[2], num_before=5, num_after=3)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], True)
        messages_matches_ids(messages, message_ids[5:8])

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[2], num_before=10, num_after=10)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], True)
        messages_matches_ids(messages, message_ids[5:])

        with first_visible_id_as(message_ids[9] + 1):
            data = self.get_messages_response(anchor=message_ids[5], num_before=5, num_after=4)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], True)
        self.assert_length(messages, 0)

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[5], num_before=0, num_after=0)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], True)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)
        messages_matches_ids(messages, message_ids[5:6])

        with first_visible_id_as(message_ids[5]):
            data = self.get_messages_response(anchor=message_ids[2], num_before=0, num_after=0)

        messages = data['messages']
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], False)
        self.assertEqual(data['history_limited'], False)
        self.assert_length(messages, 0)

        # Verify some additional behavior of found_newest.
        with first_visible_id_as(0):
            data = self.get_messages_response(anchor=LARGER_THAN_MAX_MESSAGE_ID, num_before=5, num_after=0)

        messages = data['messages']
        self.assert_length(messages, 5)
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)

        # The anchor value of 'last' behaves just like LARGER_THAN_MAX_MESSAGE_ID.
        with first_visible_id_as(0):
            data = self.get_messages_response(anchor='newest', num_before=5, num_after=0)

        messages = data['messages']
        self.assert_length(messages, 5)
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)

        with first_visible_id_as(0):
            data = self.get_messages_response(anchor=LARGER_THAN_MAX_MESSAGE_ID + 1,
                                              num_before=5, num_after=0)

        messages = data['messages']
        self.assert_length(messages, 5)
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], False)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)

        with first_visible_id_as(0):
            data = self.get_messages_response(anchor=LARGER_THAN_MAX_MESSAGE_ID, num_before=20, num_after=0)

        messages = data['messages']
        self.assert_length(messages, 10)
        self.assertEqual(data['found_anchor'], False)
        self.assertEqual(data['found_oldest'], True)
        self.assertEqual(data['found_newest'], True)
        self.assertEqual(data['history_limited'], False)

    def test_missing_params(self) -> None:
        """
        anchor, num_before, and num_after are all required
        POST parameters for get_messages.
        """
        self.login('hamlet')

        required_args: Tuple[Tuple[str, int], ...] = (("num_before", 1), ("num_after", 1))

        for i in range(len(required_args)):
            post_params = dict(required_args[:i] + required_args[i + 1:])
            result = self.client_get("/json/messages", post_params)
            self.assert_json_error(result,
                                   f"Missing '{required_args[i][0]}' argument")

    def test_get_messages_limits(self) -> None:
        """
        A call to GET /json/messages requesting more than
        MAX_MESSAGES_PER_FETCH messages returns an error message.
        """
        self.login('hamlet')
        result = self.client_get("/json/messages", dict(anchor=1, num_before=3000, num_after=3000))
        self.assert_json_error(result, "Too many messages requested (maximum 5000).")
        result = self.client_get("/json/messages", dict(anchor=1, num_before=6000, num_after=0))
        self.assert_json_error(result, "Too many messages requested (maximum 5000).")
        result = self.client_get("/json/messages", dict(anchor=1, num_before=0, num_after=6000))
        self.assert_json_error(result, "Too many messages requested (maximum 5000).")

    def test_bad_int_params(self) -> None:
        """
        num_before, num_after, and narrow must all be non-negative
        integers or strings that can be converted to non-negative integers.
        """
        self.login('hamlet')

        other_params = [("narrow", {}), ("anchor", 0)]
        int_params = ["num_before", "num_after"]

        bad_types = (False, "", "-1", -1)
        for idx, param in enumerate(int_params):
            for type in bad_types:
                # Rotate through every bad type for every integer
                # parameter, one at a time.
                post_params = dict(other_params + [(param, type)] +
                                   [(other_param, 0) for other_param in
                                    int_params[:idx] + int_params[idx + 1:]],
                                   )
                result = self.client_get("/json/messages", post_params)
                self.assert_json_error(result,
                                       f"Bad value for '{param}': {type}")

    def test_bad_narrow_type(self) -> None:
        """
        narrow must be a list of string pairs.
        """
        self.login('hamlet')

        other_params: List[Tuple[str, Union[int, str, bool]]] = [("anchor", 0), ("num_before", 0), ("num_after", 0)]

        bad_types: Tuple[Union[int, str, bool], ...] = (
            False, 0, '', '{malformed json,',
            '{foo: 3}', '[1,2]', '[["x","y","z"]]',
        )
        for type in bad_types:
            post_params = dict(other_params + [("narrow", type)])
            result = self.client_get("/json/messages", post_params)
            self.assert_json_error(result,
                                   f"Bad value for 'narrow': {type}")

    def test_bad_narrow_operator(self) -> None:
        """
        Unrecognized narrow operators are rejected.
        """
        self.login('hamlet')
        for operator in ['', 'foo', 'stream:verona', '__init__']:
            narrow = [dict(operator=operator, operand='')]
            params = dict(anchor=0, num_before=0, num_after=0, narrow=ujson.dumps(narrow))
            result = self.client_get("/json/messages", params)
            self.assert_json_error_contains(result,
                                            "Invalid narrow operator: unknown operator")

    def test_invalid_narrow_operand_in_dict(self) -> None:
        self.login('hamlet')

        # str or int is required for sender, group-pm-with, stream
        invalid_operands = [['1'], [2], None]
        error_msg = 'elem["operand"] is not a string or integer'
        for operand in ['sender', 'group-pm-with', 'stream']:
            self.exercise_bad_narrow_operand_using_dict_api(operand, invalid_operands, error_msg)

        # str or int list is required for pm-with operator
        invalid_operands = [None]
        error_msg = 'elem["operand"] is not a string or an integer list'
        self.exercise_bad_narrow_operand_using_dict_api('pm-with', invalid_operands, error_msg)

        invalid_operands = [['2']]
        error_msg = 'elem["operand"][0] is not an integer'
        self.exercise_bad_narrow_operand_using_dict_api('pm-with', invalid_operands, error_msg)

        # For others only str is acceptable
        invalid_operands = [2, None, [1]]
        error_msg = 'elem["operand"] is not a string'
        for operand in ['is', 'near', 'has', 'id']:
            self.exercise_bad_narrow_operand_using_dict_api(operand, invalid_operands, error_msg)

        # Disallow empty search terms
        error_msg = 'elem["operand"] cannot be blank.'
        self.exercise_bad_narrow_operand_using_dict_api('search', [''], error_msg)

    # The exercise_bad_narrow_operand helper method uses legacy tuple format to
    # test bad narrow, this method uses the current dict api format
    def exercise_bad_narrow_operand_using_dict_api(self, operator: str,
                                                   operands: Sequence[Any],
                                                   error_msg: str) -> None:

        for operand in operands:
            narrow = [dict(operator=operator, operand=operand)]
            params = dict(anchor=0, num_before=0, num_after=0, narrow=ujson.dumps(narrow))
            result = self.client_get('/json/messages', params)
            self.assert_json_error_contains(result, error_msg)

    def exercise_bad_narrow_operand(self, operator: str,
                                    operands: Sequence[Any],
                                    error_msg: str) -> None:
        other_params: List[Tuple[str, Any]] = [("anchor", 0), ("num_before", 0), ("num_after", 0)]
        for operand in operands:
            post_params = dict(other_params + [
                ("narrow", ujson.dumps([[operator, operand]]))])
            result = self.client_get("/json/messages", post_params)
            self.assert_json_error_contains(result, error_msg)

    def test_bad_narrow_stream_content(self) -> None:
        """
        If an invalid stream name is requested in get_messages, an error is
        returned.
        """
        self.login('hamlet')
        bad_stream_content: Tuple[int, List[None], List[str]] = (0, [], ["x", "y"])
        self.exercise_bad_narrow_operand("stream", bad_stream_content,
                                         "Bad value for 'narrow'")

    def test_bad_narrow_one_on_one_email_content(self) -> None:
        """
        If an invalid 'pm-with' is requested in get_messages, an
        error is returned.
        """
        self.login('hamlet')
        bad_stream_content: Tuple[int, List[None], List[str]] = (0, [], ["x", "y"])
        self.exercise_bad_narrow_operand("pm-with", bad_stream_content,
                                         "Bad value for 'narrow'")

    def test_bad_narrow_nonexistent_stream(self) -> None:
        self.login('hamlet')
        self.exercise_bad_narrow_operand("stream", ['non-existent stream'],
                                         "Invalid narrow operator: unknown stream")

        non_existing_stream_id = 1232891381239
        self.exercise_bad_narrow_operand_using_dict_api('stream', [non_existing_stream_id],
                                                        'Invalid narrow operator: unknown stream')

    def test_bad_narrow_nonexistent_email(self) -> None:
        self.login('hamlet')
        self.exercise_bad_narrow_operand("pm-with", ['non-existent-user@zulip.com'],
                                         "Invalid narrow operator: unknown user")

    def test_bad_narrow_pm_with_id_list(self) -> None:
        self.login('hamlet')
        self.exercise_bad_narrow_operand('pm-with', [-24],
                                         "Bad value for 'narrow': [[\"pm-with\",-24]]")

    def test_message_without_rendered_content(self) -> None:
        """Older messages may not have rendered_content in the database"""
        m = self.get_last_message()
        m.rendered_content = m.rendered_content_version = None
        m.content = 'test content'
        wide_dict = MessageDict.wide_dict(m)
        final_dict = MessageDict.finalize_payload(
            wide_dict,
            apply_markdown=True,
            client_gravatar=False,
        )
        self.assertEqual(final_dict['content'], '<p>test content</p>')

    def common_check_get_messages_query(self, query_params: Dict[str, object], expected: str) -> None:
        user_profile = self.example_user('hamlet')
        request = POSTRequestMock(query_params, user_profile)
        with queries_captured() as queries:
            get_messages_backend(request, user_profile)

        for query in queries:
            if "/* get_messages */" in query['sql']:
                sql = str(query['sql']).replace(" /* get_messages */", '')
                self.assertEqual(sql, expected)
                return
        raise AssertionError("get_messages query not found")

    def test_find_first_unread_anchor(self) -> None:
        hamlet = self.example_user('hamlet')
        cordelia = self.example_user('cordelia')
        othello = self.example_user('othello')

        self.make_stream('England')

        # Send a few messages that Hamlet won't have UserMessage rows for.
        unsub_message_id = self.send_stream_message(cordelia, 'England')
        self.send_personal_message(cordelia, othello)

        self.subscribe(hamlet, 'England')

        muted_topics = [
            ['England', 'muted'],
        ]
        set_topic_mutes(hamlet, muted_topics)

        # send a muted message
        muted_message_id = self.send_stream_message(cordelia, 'England', topic_name='muted')

        # finally send Hamlet a "normal" message
        first_message_id = self.send_stream_message(cordelia, 'England')

        # send a few more messages
        extra_message_id = self.send_stream_message(cordelia, 'England')
        self.send_personal_message(cordelia, hamlet)

        sa_conn = get_sqlalchemy_connection()

        user_profile = hamlet

        anchor = find_first_unread_anchor(
            sa_conn=sa_conn,
            user_profile=user_profile,
            narrow=[],
        )
        self.assertEqual(anchor, first_message_id)

        # With the same data setup, we now want to test that a reasonable
        # search still gets the first message sent to Hamlet (before he
        # subscribed) and other recent messages to the stream.
        query_params = dict(
            anchor="first_unread",
            num_before=10,
            num_after=10,
            narrow='[["stream", "England"]]',
        )
        request = POSTRequestMock(query_params, user_profile)

        payload = get_messages_backend(request, user_profile)
        result = ujson.loads(payload.content)
        self.assertEqual(result['anchor'], first_message_id)
        self.assertEqual(result['found_newest'], True)
        self.assertEqual(result['found_oldest'], True)

        messages = result['messages']
        self.assertEqual(
            {msg['id'] for msg in messages},
            {unsub_message_id, muted_message_id, first_message_id, extra_message_id},
        )

    def test_use_first_unread_anchor_with_some_unread_messages(self) -> None:
        user_profile = self.example_user('hamlet')

        # Have Othello send messages to Hamlet that he hasn't read.
        # Here, Hamlet isn't subscribed to the stream Scotland
        self.send_stream_message(self.example_user("othello"), "Scotland")
        first_unread_message_id = self.send_personal_message(
            self.example_user("othello"),
            self.example_user("hamlet"),
        )

        # Add a few messages that help us test that our query doesn't
        # look at messages that are irrelevant to Hamlet.
        self.send_personal_message(self.example_user("othello"), self.example_user("cordelia"))
        self.send_personal_message(self.example_user("othello"), self.example_user("iago"))

        query_params = dict(
            anchor="first_unread",
            num_before=10,
            num_after=10,
            narrow='[]',
        )
        request = POSTRequestMock(query_params, user_profile)

        with queries_captured() as all_queries:
            get_messages_backend(request, user_profile)

        # Verify the query for old messages looks correct.
        queries = [q for q in all_queries if '/* get_messages */' in q['sql']]
        self.assertEqual(len(queries), 1)
        sql = queries[0]['sql']
        self.assertNotIn(f'AND message_id = {LARGER_THAN_MAX_MESSAGE_ID}', sql)
        self.assertIn('ORDER BY message_id ASC', sql)

        cond = f'WHERE user_profile_id = {user_profile.id} AND message_id >= {first_unread_message_id}'
        self.assertIn(cond, sql)
        cond = f'WHERE user_profile_id = {user_profile.id} AND message_id <= {first_unread_message_id - 1}'
        self.assertIn(cond, sql)
        self.assertIn('UNION', sql)

    def test_visible_messages_use_first_unread_anchor_with_some_unread_messages(self) -> None:
        user_profile = self.example_user('hamlet')

        # Have Othello send messages to Hamlet that he hasn't read.
        self.subscribe(self.example_user("hamlet"), 'Scotland')

        first_unread_message_id = self.send_stream_message(self.example_user("othello"), "Scotland")
        self.send_stream_message(self.example_user("othello"), "Scotland")
        self.send_stream_message(self.example_user("othello"), "Scotland")
        self.send_personal_message(
            self.example_user("othello"),
            self.example_user("hamlet"),
        )

        # Add a few messages that help us test that our query doesn't
        # look at messages that are irrelevant to Hamlet.
        self.send_personal_message(self.example_user("othello"), self.example_user("cordelia"))
        self.send_personal_message(self.example_user("othello"), self.example_user("iago"))

        query_params = dict(
            anchor="first_unread",
            num_before=10,
            num_after=10,
            narrow='[]',
        )
        request = POSTRequestMock(query_params, user_profile)

        first_visible_message_id = first_unread_message_id + 2
        with first_visible_id_as(first_visible_message_id):
            with queries_captured() as all_queries:
                get_messages_backend(request, user_profile)

        queries = [q for q in all_queries if '/* get_messages */' in q['sql']]
        self.assertEqual(len(queries), 1)
        sql = queries[0]['sql']
        self.assertNotIn(f'AND message_id = {LARGER_THAN_MAX_MESSAGE_ID}', sql)
        self.assertIn('ORDER BY message_id ASC', sql)
        cond = f'WHERE user_profile_id = {user_profile.id} AND message_id <= {first_unread_message_id - 1}'
        self.assertIn(cond, sql)
        cond = f'WHERE user_profile_id = {user_profile.id} AND message_id >= {first_visible_message_id}'
        self.assertIn(cond, sql)

    def test_use_first_unread_anchor_with_no_unread_messages(self) -> None:
        user_profile = self.example_user('hamlet')

        query_params = dict(
            anchor="first_unread",
            num_before=10,
            num_after=10,
            narrow='[]',
        )
        request = POSTRequestMock(query_params, user_profile)

        with queries_captured() as all_queries:
            get_messages_backend(request, user_profile)

        queries = [q for q in all_queries if '/* get_messages */' in q['sql']]
        self.assertEqual(len(queries), 1)

        sql = queries[0]['sql']

        self.assertNotIn('AND message_id <=', sql)
        self.assertNotIn('AND message_id >=', sql)

        first_visible_message_id = 5
        with first_visible_id_as(first_visible_message_id):
            with queries_captured() as all_queries:
                get_messages_backend(request, user_profile)
            queries = [q for q in all_queries if '/* get_messages */' in q['sql']]
            sql = queries[0]['sql']
            self.assertNotIn('AND message_id <=', sql)
            self.assertNotIn('AND message_id >=', sql)

    def test_use_first_unread_anchor_with_muted_topics(self) -> None:
        """
        Test that our logic related to `use_first_unread_anchor`
        invokes the `message_id = LARGER_THAN_MAX_MESSAGE_ID` hack for
        the `/* get_messages */` query when relevant muting
        is in effect.

        This is a very arcane test on arcane, but very heavily
        field-tested, logic in get_messages_backend().  If
        this test breaks, be absolutely sure you know what you're
        doing.
        """

        realm = get_realm('zulip')
        self.make_stream('web stuff')
        self.make_stream('bogus')
        user_profile = self.example_user('hamlet')
        muted_topics = [
            ['Scotland', 'golf'],
            ['web stuff', 'css'],
            ['bogus', 'bogus'],
        ]
        set_topic_mutes(user_profile, muted_topics)

        query_params = dict(
            anchor="first_unread",
            num_before=0,
            num_after=0,
            narrow='[["stream", "Scotland"]]',
        )
        request = POSTRequestMock(query_params, user_profile)

        with queries_captured() as all_queries:
            get_messages_backend(request, user_profile)

        # Do some tests on the main query, to verify the muting logic
        # runs on this code path.
        queries = [q for q in all_queries if str(q['sql']).startswith("SELECT message_id, flags")]
        self.assertEqual(len(queries), 1)

        stream = get_stream('Scotland', realm)
        recipient_id = stream.recipient.id
        cond = f"AND NOT (recipient_id = {recipient_id} AND upper(subject) = upper('golf'))"
        self.assertIn(cond, queries[0]['sql'])

        # Next, verify the use_first_unread_anchor setting invokes
        # the `message_id = LARGER_THAN_MAX_MESSAGE_ID` hack.
        queries = [q for q in all_queries if '/* get_messages */' in q['sql']]
        self.assertEqual(len(queries), 1)
        self.assertIn(f'AND zerver_message.id = {LARGER_THAN_MAX_MESSAGE_ID}',
                      queries[0]['sql'])

    def test_exclude_muting_conditions(self) -> None:
        realm = get_realm('zulip')
        self.make_stream('web stuff')
        user_profile = self.example_user('hamlet')

        self.make_stream('irrelevant_stream')

        # Test the do-nothing case first.
        muted_topics = [
            ['irrelevant_stream', 'irrelevant_topic'],
        ]
        set_topic_mutes(user_profile, muted_topics)

        # If nothing relevant is muted, then exclude_muting_conditions()
        # should return an empty list.
        narrow: List[Dict[str, object]] = [
            dict(operator='stream', operand='Scotland'),
        ]
        muting_conditions = exclude_muting_conditions(user_profile, narrow)
        self.assertEqual(muting_conditions, [])

        # Also test that passing stream ID works
        narrow = [
            dict(operator='stream', operand=get_stream('Scotland', realm).id),
        ]
        muting_conditions = exclude_muting_conditions(user_profile, narrow)
        self.assertEqual(muting_conditions, [])

        # Ok, now set up our muted topics to include a topic relevant to our narrow.
        muted_topics = [
            ['Scotland', 'golf'],
            ['web stuff', 'css'],
        ]
        set_topic_mutes(user_profile, muted_topics)

        # And verify that our query will exclude them.
        narrow = [
            dict(operator='stream', operand='Scotland'),
        ]

        muting_conditions = exclude_muting_conditions(user_profile, narrow)
        query = select([column("id").label("message_id")], None, table("zerver_message"))
        query = query.where(*muting_conditions)
        expected_query = '''\
SELECT id AS message_id \n\
FROM zerver_message \n\
WHERE NOT (recipient_id = %(recipient_id_1)s AND upper(subject) = upper(%(param_1)s))\
'''

        self.assertEqual(get_sqlalchemy_sql(query), expected_query)
        params = get_sqlalchemy_query_params(query)

        self.assertEqual(params['recipient_id_1'], get_recipient_id_for_stream_name(realm, 'Scotland'))
        self.assertEqual(params['param_1'], 'golf')

        mute_stream(realm, user_profile, 'Verona')

        # Using a bogus stream name should be similar to using no narrow at
        # all, and we'll exclude all mutes.
        narrow = [
            dict(operator='stream', operand='bogus-stream-name'),
        ]

        muting_conditions = exclude_muting_conditions(user_profile, narrow)
        query = select([column("id")], None, table("zerver_message"))
        query = query.where(and_(*muting_conditions))

        expected_query = '''\
SELECT id \n\
FROM zerver_message \n\
WHERE recipient_id NOT IN (%(recipient_id_1)s) \
AND NOT \
(recipient_id = %(recipient_id_2)s AND upper(subject) = upper(%(param_1)s) OR \
recipient_id = %(recipient_id_3)s AND upper(subject) = upper(%(param_2)s))\
'''
        self.assertEqual(get_sqlalchemy_sql(query), expected_query)
        params = get_sqlalchemy_query_params(query)
        self.assertEqual(params['recipient_id_1'], get_recipient_id_for_stream_name(realm, 'Verona'))
        self.assertEqual(params['recipient_id_2'], get_recipient_id_for_stream_name(realm, 'Scotland'))
        self.assertEqual(params['param_1'], 'golf')
        self.assertEqual(params['recipient_id_3'], get_recipient_id_for_stream_name(realm, 'web stuff'))
        self.assertEqual(params['param_2'], 'css')

    def test_get_messages_queries(self) -> None:
        query_ids = self.get_query_ids()

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} AND message_id = 0) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 0}, sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} AND message_id = 0) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 1, 'num_after': 0}, sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} ORDER BY message_id ASC \n LIMIT 2) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 1}, sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} ORDER BY message_id ASC \n LIMIT 11) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 10}, sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} AND message_id <= 100 ORDER BY message_id DESC \n LIMIT 11) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 100, 'num_before': 10, 'num_after': 0}, sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM ((SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} AND message_id <= 99 ORDER BY message_id DESC \n LIMIT 10) UNION ALL (SELECT message_id, flags \nFROM zerver_usermessage \nWHERE user_profile_id = {hamlet_id} AND message_id >= 100 ORDER BY message_id ASC \n LIMIT 11)) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 100, 'num_before': 10, 'num_after': 10}, sql)

    def test_get_messages_with_narrow_queries(self) -> None:
        query_ids = self.get_query_ids()
        hamlet_email = self.example_user('hamlet').email
        othello_email = self.example_user('othello').email

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND (sender_id = {othello_id} AND recipient_id = {hamlet_recipient} OR sender_id = {hamlet_id} AND recipient_id = {othello_recipient}) AND message_id = 0) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 0,
                                              'narrow': f'[["pm-with", "{othello_email}"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND (sender_id = {othello_id} AND recipient_id = {hamlet_recipient} OR sender_id = {hamlet_id} AND recipient_id = {othello_recipient}) AND message_id = 0) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 1, 'num_after': 0,
                                              'narrow': f'[["pm-with", "{othello_email}"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND (sender_id = {othello_id} AND recipient_id = {hamlet_recipient} OR sender_id = {hamlet_id} AND recipient_id = {othello_recipient}) ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': f'[["pm-with", "{othello_email}"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND (flags & 2) != 0 ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["is", "starred"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND sender_id = {othello_id} ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': f'[["sender", "{othello_email}"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id \nFROM (SELECT id AS message_id \nFROM zerver_message \nWHERE recipient_id = {scotland_recipient} ORDER BY zerver_message.id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["stream", "Scotland"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id \nFROM (SELECT id AS message_id \nFROM zerver_message \nWHERE recipient_id IN ({public_streams_recipents}) ORDER BY zerver_message.id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["streams", "public"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND recipient_id NOT IN ({public_streams_recipents}) ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[{"operator":"streams", "operand":"public", "negated": true}]'},
                                             sql)

        sql_template = "SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND upper(subject) = upper('blah') ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC"
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["topic", "blah"]]'},
                                             sql)

        sql_template = "SELECT anon_1.message_id \nFROM (SELECT id AS message_id \nFROM zerver_message \nWHERE recipient_id = {scotland_recipient} AND upper(subject) = upper('blah') ORDER BY zerver_message.id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC"
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["stream", "Scotland"], ["topic", "blah"]]'},
                                             sql)

        # Narrow to pms with yourself
        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND sender_id = {hamlet_id} AND recipient_id = {hamlet_recipient} ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': f'[["pm-with", "{hamlet_email}"]]'},
                                             sql)

        sql_template = 'SELECT anon_1.message_id, anon_1.flags \nFROM (SELECT message_id, flags \nFROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \nWHERE user_profile_id = {hamlet_id} AND recipient_id = {scotland_recipient} AND (flags & 2) != 0 ORDER BY message_id ASC \n LIMIT 10) AS anon_1 ORDER BY message_id ASC'
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["stream", "Scotland"], ["is", "starred"]]'},
                                             sql)

    @override_settings(USING_PGROONGA=False)
    def test_get_messages_with_search_queries(self) -> None:
        query_ids = self.get_query_ids()

        sql_template = """\
SELECT anon_1.message_id, anon_1.flags, anon_1.subject, anon_1.rendered_content, anon_1.content_matches, anon_1.topic_matches \n\
FROM (SELECT message_id, flags, subject, rendered_content, array((SELECT ARRAY[sum(length(anon_3) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_3, '</ts-match>') - 1] AS anon_2 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', rendered_content, plainto_tsquery('zulip.english_us_search', 'jumping'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_3 \n\
 LIMIT ALL OFFSET 1)) AS content_matches, array((SELECT ARRAY[sum(length(anon_5) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_5, '</ts-match>') - 1] AS anon_4 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', escape_html(subject), plainto_tsquery('zulip.english_us_search', 'jumping'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_5 \n\
 LIMIT ALL OFFSET 1)) AS topic_matches \n\
FROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \n\
WHERE user_profile_id = {hamlet_id} AND (search_tsvector @@ plainto_tsquery('zulip.english_us_search', 'jumping')) ORDER BY message_id ASC \n\
 LIMIT 10) AS anon_1 ORDER BY message_id ASC\
"""
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["search", "jumping"]]'},
                                             sql)

        sql_template = """\
SELECT anon_1.message_id, anon_1.subject, anon_1.rendered_content, anon_1.content_matches, anon_1.topic_matches \n\
FROM (SELECT id AS message_id, subject, rendered_content, array((SELECT ARRAY[sum(length(anon_3) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_3, '</ts-match>') - 1] AS anon_2 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', rendered_content, plainto_tsquery('zulip.english_us_search', 'jumping'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_3 \n\
 LIMIT ALL OFFSET 1)) AS content_matches, array((SELECT ARRAY[sum(length(anon_5) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_5, '</ts-match>') - 1] AS anon_4 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', escape_html(subject), plainto_tsquery('zulip.english_us_search', 'jumping'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_5 \n\
 LIMIT ALL OFFSET 1)) AS topic_matches \n\
FROM zerver_message \n\
WHERE recipient_id = {scotland_recipient} AND (search_tsvector @@ plainto_tsquery('zulip.english_us_search', 'jumping')) ORDER BY zerver_message.id ASC \n\
 LIMIT 10) AS anon_1 ORDER BY message_id ASC\
"""
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["stream", "Scotland"], ["search", "jumping"]]'},
                                             sql)

        sql_template = """\
SELECT anon_1.message_id, anon_1.flags, anon_1.subject, anon_1.rendered_content, anon_1.content_matches, anon_1.topic_matches \n\
FROM (SELECT message_id, flags, subject, rendered_content, array((SELECT ARRAY[sum(length(anon_3) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_3, '</ts-match>') - 1] AS anon_2 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', rendered_content, plainto_tsquery('zulip.english_us_search', '"jumping" quickly'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_3 \n\
 LIMIT ALL OFFSET 1)) AS content_matches, array((SELECT ARRAY[sum(length(anon_5) - 11) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) + 11, strpos(anon_5, '</ts-match>') - 1] AS anon_4 \n\
FROM unnest(string_to_array(ts_headline('zulip.english_us_search', escape_html(subject), plainto_tsquery('zulip.english_us_search', '"jumping" quickly'), 'HighlightAll = TRUE, StartSel = <ts-match>, StopSel = </ts-match>'), '<ts-match>')) AS anon_5 \n\
 LIMIT ALL OFFSET 1)) AS topic_matches \n\
FROM zerver_usermessage JOIN zerver_message ON zerver_usermessage.message_id = zerver_message.id \n\
WHERE user_profile_id = {hamlet_id} AND (content ILIKE '%jumping%' OR subject ILIKE '%jumping%') AND (search_tsvector @@ plainto_tsquery('zulip.english_us_search', '"jumping" quickly')) ORDER BY message_id ASC \n\
 LIMIT 10) AS anon_1 ORDER BY message_id ASC\
"""
        sql = sql_template.format(**query_ids)
        self.common_check_get_messages_query({'anchor': 0, 'num_before': 0, 'num_after': 9,
                                              'narrow': '[["search", "\\"jumping\\" quickly"]]'},
                                             sql)

    @override_settings(USING_PGROONGA=False)
    def test_get_messages_with_search_using_email(self) -> None:
        self.login('cordelia')

        othello = self.example_user('othello')
        cordelia = self.example_user('cordelia')

        messages_to_search = [
            ('say hello', 'How are you doing, @**Othello, the Moor of Venice**?'),
            ('lunch plans', 'I am hungry!'),
        ]
        next_message_id = self.get_last_message().id + 1

        for topic, content in messages_to_search:
            self.send_stream_message(
                sender=cordelia,
                stream_name="Verona",
                content=content,
                topic_name=topic,
            )

        self._update_tsvector_index()

        narrow = [
            dict(operator='sender', operand=cordelia.email),
            dict(operator='search', operand=othello.email),
        ]
        result: Dict[str, Any] = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_after=10,
        ))
        self.assertEqual(len(result['messages']), 0)

        narrow = [
            dict(operator='sender', operand=cordelia.email),
            dict(operator='search', operand='othello'),
        ]
        result = self.get_and_check_messages(dict(
            narrow=ujson.dumps(narrow),
            anchor=next_message_id,
            num_after=10,
        ))
        self.assertEqual(len(result['messages']), 1)
        messages = result['messages']

        (hello_message,) = [
            m for m in messages
            if m[TOPIC_NAME] == 'say hello'
        ]
        self.assertEqual(
            hello_message[MATCH_TOPIC],
            'say hello')
        self.assertEqual(
            hello_message['match_content'],
            f'<p>How are you doing, <span class="user-mention" data-user-id="{othello.id}">'
            '@<span class="highlight">Othello</span>, the Moor of Venice</span>?</p>',
        )
