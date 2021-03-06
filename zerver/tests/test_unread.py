from typing import Any, List, Mapping, Set
from unittest import mock

import ujson
from django.db import connection

from zerver.lib.fix_unreads import fix, fix_unsubscribed
from zerver.lib.message import (
    MessageDict,
    UnreadMessagesResult,
    aggregate_unread_data,
    apply_unread_message_event,
    get_raw_unread_data,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import get_subscription, tornado_redirected_to_list
from zerver.lib.topic_mutes import add_topic_mute
from zerver.models import (
    Message,
    Recipient,
    Stream,
    Subscription,
    UserMessage,
    UserProfile,
    get_realm,
    get_stream,
)


class FirstUnreadAnchorTests(ZulipTestCase):
    '''
    HISTORICAL NOTE:

    The two tests in this class were originally written when
    we had the concept of a "pointer", and they may be a bit
    redundant in what they now check.
    '''
    def test_use_first_unread_anchor(self) -> None:
        self.login('hamlet')

        # Mark all existing messages as read
        result = self.client_post("/json/mark_all_as_read")
        self.assert_json_success(result)

        # Send a new message (this will be unread)
        new_message_id = self.send_stream_message(self.example_user("othello"), "Verona",
                                                  "test")

        # If we call get_messages with use_first_unread_anchor=True, we
        # should get the message we just sent
        messages_response = self.get_messages_response(
            anchor="first_unread", num_before=0, num_after=1)
        self.assertEqual(messages_response['messages'][0]['id'], new_message_id)
        self.assertEqual(messages_response['anchor'], new_message_id)

        # Test with the old way of expressing use_first_unread_anchor=True
        messages_response = self.get_messages_response(
            anchor=0, num_before=0, num_after=1, use_first_unread_anchor=True)
        self.assertEqual(messages_response['messages'][0]['id'], new_message_id)
        self.assertEqual(messages_response['anchor'], new_message_id)

        # We want to get the message_id of an arbitrary old message. We can
        # call get_messages with use_first_unread_anchor=False and simply
        # save the first message we're returned.
        messages = self.get_messages(
            anchor=0, num_before=0, num_after=2, use_first_unread_anchor=False)
        old_message_id = messages[0]['id']

        # Verify the message is marked as read
        user_message = UserMessage.objects.get(
            message_id=old_message_id,
            user_profile=self.example_user('hamlet'))
        self.assertTrue(user_message.flags.read)

        # Let's set this old message to be unread
        result = self.client_post("/json/messages/flags",
                                  {"messages": ujson.dumps([old_message_id]),
                                   "op": "remove",
                                   "flag": "read"})

        # Verify it's now marked as unread
        user_message = UserMessage.objects.get(
            message_id=old_message_id,
            user_profile=self.example_user('hamlet'))
        self.assert_json_success(result)
        self.assertFalse(user_message.flags.read)

        # Now if we call get_messages with use_first_unread_anchor=True,
        # we should get the old message we just set to unread
        messages_response = self.get_messages_response(
            anchor="first_unread", num_before=0, num_after=1)
        self.assertEqual(messages_response['messages'][0]['id'], old_message_id)
        self.assertEqual(messages_response['anchor'], old_message_id)

    def test_visible_messages_use_first_unread_anchor(self) -> None:
        self.login('hamlet')

        result = self.client_post("/json/mark_all_as_read")
        self.assert_json_success(result)

        new_message_id = self.send_stream_message(self.example_user("othello"), "Verona",
                                                  "test")

        messages_response = self.get_messages_response(
            anchor="first_unread", num_before=0, num_after=1)
        self.assertEqual(messages_response['messages'][0]['id'], new_message_id)
        self.assertEqual(messages_response['anchor'], new_message_id)

        with mock.patch('zerver.views.message_fetch.get_first_visible_message_id',
                        return_value=new_message_id):
            messages_response = self.get_messages_response(
                anchor="first_unread", num_before=0, num_after=1)
        self.assertEqual(messages_response['messages'][0]['id'], new_message_id)
        self.assertEqual(messages_response['anchor'], new_message_id)

        with mock.patch('zerver.views.message_fetch.get_first_visible_message_id',
                        return_value=new_message_id + 1):
            messages_reponse = self.get_messages_response(
                anchor="first_unread", num_before=0, num_after=1)
        self.assert_length(messages_reponse['messages'], 0)
        self.assertIn('anchor', messages_reponse)

        with mock.patch('zerver.views.message_fetch.get_first_visible_message_id',
                        return_value=new_message_id - 1):
            messages = self.get_messages(
                anchor="first_unread", num_before=0, num_after=1)
        self.assert_length(messages, 1)


class UnreadCountTests(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        with mock.patch('zerver.lib.push_notifications.push_notifications_enabled',
                        return_value = True) as mock_push_notifications_enabled:
            self.unread_msg_ids = [
                self.send_personal_message(
                    self.example_user("iago"), self.example_user("hamlet"), "hello"),
                self.send_personal_message(
                    self.example_user("iago"), self.example_user("hamlet"), "hello2")]
            mock_push_notifications_enabled.assert_called()

    # Sending a new message results in unread UserMessages being created
    def test_new_message(self) -> None:
        self.login('hamlet')
        content = "Test message for unset read bit"
        last_msg = self.send_stream_message(self.example_user("hamlet"), "Verona", content)
        user_messages = list(UserMessage.objects.filter(message=last_msg))
        self.assertEqual(len(user_messages) > 0, True)
        for um in user_messages:
            self.assertEqual(um.message.content, content)
            if um.user_profile.email != self.example_email("hamlet"):
                self.assertFalse(um.flags.read)

    def test_update_flags(self) -> None:
        self.login('hamlet')

        result = self.client_post("/json/messages/flags",
                                  {"messages": ujson.dumps(self.unread_msg_ids),
                                   "op": "add",
                                   "flag": "read"})
        self.assert_json_success(result)

        # Ensure we properly set the flags
        found = 0
        for msg in self.get_messages():
            if msg['id'] in self.unread_msg_ids:
                self.assertEqual(msg['flags'], ['read'])
                found += 1
        self.assertEqual(found, 2)

        result = self.client_post("/json/messages/flags",
                                  {"messages": ujson.dumps([self.unread_msg_ids[1]]),
                                   "op": "remove", "flag": "read"})
        self.assert_json_success(result)

        # Ensure we properly remove just one flag
        for msg in self.get_messages():
            if msg['id'] == self.unread_msg_ids[0]:
                self.assertEqual(msg['flags'], ['read'])
            elif msg['id'] == self.unread_msg_ids[1]:
                self.assertEqual(msg['flags'], [])

    def test_mark_all_in_stream_read(self) -> None:
        self.login('hamlet')
        user_profile = self.example_user('hamlet')
        stream = self.subscribe(user_profile, "test_stream")
        self.subscribe(self.example_user("cordelia"), "test_stream")

        message_id = self.send_stream_message(self.example_user("hamlet"), "test_stream", "hello")
        unrelated_message_id = self.send_stream_message(self.example_user("hamlet"), "Denmark", "hello")

        events: List[Mapping[str, Any]] = []
        with tornado_redirected_to_list(events):
            result = self.client_post("/json/mark_stream_as_read", {
                "stream_id": stream.id,
            })

        self.assert_json_success(result)
        self.assertTrue(len(events) == 1)

        event = events[0]['event']
        expected = dict(operation='add',
                        messages=[message_id],
                        flag='read',
                        type='update_message_flags',
                        all=False)

        differences = [key for key in expected if expected[key] != event[key]]
        self.assertTrue(len(differences) == 0)

        hamlet = self.example_user('hamlet')
        um = list(UserMessage.objects.filter(message=message_id))
        for msg in um:
            if msg.user_profile.email == hamlet.email:
                self.assertTrue(msg.flags.read)
            else:
                self.assertFalse(msg.flags.read)

        unrelated_messages = list(UserMessage.objects.filter(message=unrelated_message_id))
        for msg in unrelated_messages:
            if msg.user_profile.email == hamlet.email:
                self.assertFalse(msg.flags.read)

    def test_mark_all_in_invalid_stream_read(self) -> None:
        self.login('hamlet')
        invalid_stream_id = "12345678"
        result = self.client_post("/json/mark_stream_as_read", {
            "stream_id": invalid_stream_id,
        })
        self.assert_json_error(result, 'Invalid stream id')

    def test_mark_all_topics_unread_with_invalid_stream_name(self) -> None:
        self.login('hamlet')
        invalid_stream_id = "12345678"
        result = self.client_post("/json/mark_topic_as_read", {
            "stream_id": invalid_stream_id,
            'topic_name': 'whatever',
        })
        self.assert_json_error(result, "Invalid stream id")

    def test_mark_all_in_stream_topic_read(self) -> None:
        self.login('hamlet')
        user_profile = self.example_user('hamlet')
        self.subscribe(user_profile, "test_stream")

        message_id = self.send_stream_message(self.example_user("hamlet"), "test_stream", "hello", "test_topic")
        unrelated_message_id = self.send_stream_message(self.example_user("hamlet"), "Denmark", "hello", "Denmark2")
        events: List[Mapping[str, Any]] = []
        with tornado_redirected_to_list(events):
            result = self.client_post("/json/mark_topic_as_read", {
                "stream_id": get_stream("test_stream", user_profile.realm).id,
                "topic_name": "test_topic",
            })

        self.assert_json_success(result)
        self.assertTrue(len(events) == 1)

        event = events[0]['event']
        expected = dict(operation='add',
                        messages=[message_id],
                        flag='read',
                        type='update_message_flags',
                        all=False)

        differences = [key for key in expected if expected[key] != event[key]]
        self.assertTrue(len(differences) == 0)

        um = list(UserMessage.objects.filter(message=message_id))
        for msg in um:
            if msg.user_profile_id == user_profile.id:
                self.assertTrue(msg.flags.read)

        unrelated_messages = list(UserMessage.objects.filter(message=unrelated_message_id))
        for msg in unrelated_messages:
            if msg.user_profile_id == user_profile.id:
                self.assertFalse(msg.flags.read)

    def test_mark_all_in_invalid_topic_read(self) -> None:
        self.login('hamlet')
        invalid_topic_name = "abc"
        result = self.client_post("/json/mark_topic_as_read", {
            "stream_id": get_stream("Denmark", get_realm("zulip")).id,
            "topic_name": invalid_topic_name,
        })
        self.assert_json_error(result, 'No such topic \'abc\'')

class FixUnreadTests(ZulipTestCase):
    def test_fix_unreads(self) -> None:
        user = self.example_user('hamlet')
        realm = get_realm('zulip')

        def send_message(stream_name: str, topic_name: str) -> int:
            msg_id = self.send_stream_message(
                self.example_user("othello"),
                stream_name,
                topic_name=topic_name)
            um = UserMessage.objects.get(
                user_profile=user,
                message_id=msg_id)
            return um.id

        def assert_read(user_message_id: int) -> None:
            um = UserMessage.objects.get(id=user_message_id)
            self.assertTrue(um.flags.read)

        def assert_unread(user_message_id: int) -> None:
            um = UserMessage.objects.get(id=user_message_id)
            self.assertFalse(um.flags.read)

        def mute_stream(stream_name: str) -> None:
            stream = get_stream(stream_name, realm)
            recipient = stream.recipient
            subscription = Subscription.objects.get(
                user_profile=user,
                recipient=recipient,
            )
            subscription.is_muted = True
            subscription.save()

        def mute_topic(stream_name: str, topic_name: str) -> None:
            stream = get_stream(stream_name, realm)
            recipient = stream.recipient

            add_topic_mute(
                user_profile=user,
                stream_id=stream.id,
                recipient_id=recipient.id,
                topic_name=topic_name,
            )

        def force_unsubscribe(stream_name: str) -> None:
            '''
            We don't want side effects here, since the eventual
            unsubscribe path may mark messages as read, defeating
            the test setup here.
            '''
            sub = get_subscription(stream_name, user)
            sub.active = False
            sub.save()

        # The data setup here is kind of funny, because some of these
        # conditions should not actually happen in practice going forward,
        # but we may have had bad data from the past.

        mute_stream('Denmark')
        mute_topic('Verona', 'muted_topic')

        um_normal_id = send_message('Verona', 'normal')
        um_muted_topic_id = send_message('Verona', 'muted_topic')
        um_muted_stream_id = send_message('Denmark', 'whatever')

        self.subscribe(user, 'temporary')
        um_unsubscribed_id = send_message('temporary', 'whatever')
        force_unsubscribe('temporary')

        # Verify the setup
        assert_unread(um_normal_id)
        assert_unread(um_muted_topic_id)
        assert_unread(um_muted_stream_id)
        assert_unread(um_unsubscribed_id)

        # fix unsubscribed
        with connection.cursor() as cursor:
            fix_unsubscribed(cursor, user)

        # Muted messages don't change.
        assert_unread(um_muted_topic_id)
        assert_unread(um_muted_stream_id)
        assert_unread(um_normal_id)

        # The unsubscribed entry should change.
        assert_read(um_unsubscribed_id)

        # test idempotency
        fix(user)

        assert_unread(um_normal_id)
        assert_unread(um_muted_topic_id)
        assert_unread(um_muted_stream_id)
        assert_read(um_unsubscribed_id)

class PushNotificationMarkReadFlowsTest(ZulipTestCase):
    def get_mobile_push_notification_ids(self, user_profile: UserProfile) -> List[int]:
        return list(UserMessage.objects.filter(
            user_profile=user_profile,
        ).extra(
            where=[UserMessage.where_active_push_notification()],
        ).order_by("message_id").values_list("message_id", flat=True))

    @mock.patch('zerver.lib.push_notifications.push_notifications_enabled', return_value=True)
    def test_track_active_mobile_push_notifications(self, mock_push_notifications: mock.MagicMock) -> None:
        mock_push_notifications.return_value = True
        self.login('hamlet')
        user_profile = self.example_user('hamlet')
        stream = self.subscribe(user_profile, "test_stream")
        second_stream = self.subscribe(user_profile, "second_stream")

        property_name = "push_notifications"
        result = self.api_post(user_profile, "/api/v1/users/me/subscriptions/properties",
                               {"subscription_data": ujson.dumps([{"property": property_name,
                                                                   "value": True,
                                                                   "stream_id": stream.id}])})
        result = self.api_post(user_profile, "/api/v1/users/me/subscriptions/properties",
                               {"subscription_data": ujson.dumps([{"property": property_name,
                                                                   "value": True,
                                                                   "stream_id": second_stream.id}])})
        self.assert_json_success(result)
        self.assertEqual(self.get_mobile_push_notification_ids(user_profile), [])

        message_id = self.send_stream_message(self.example_user("cordelia"), "test_stream", "hello", "test_topic")
        second_message_id = self.send_stream_message(self.example_user("cordelia"), "test_stream", "hello", "other_topic")
        third_message_id = self.send_stream_message(self.example_user("cordelia"), "second_stream", "hello", "test_topic")

        self.assertEqual(self.get_mobile_push_notification_ids(user_profile),
                         [message_id, second_message_id, third_message_id])

        result = self.client_post("/json/mark_topic_as_read", {
            "stream_id": str(stream.id),
            "topic_name": "test_topic",
        })

        self.assert_json_success(result)
        self.assertEqual(self.get_mobile_push_notification_ids(user_profile),
                         [second_message_id, third_message_id])

        result = self.client_post("/json/mark_stream_as_read", {
            "stream_id": str(stream.id),
            "topic_name": "test_topic",
        })
        self.assertEqual(self.get_mobile_push_notification_ids(user_profile),
                         [third_message_id])

        fourth_message_id = self.send_stream_message(self.example_user("cordelia"), "test_stream", "hello", "test_topic")
        self.assertEqual(self.get_mobile_push_notification_ids(user_profile),
                         [third_message_id, fourth_message_id])

        result = self.client_post("/json/mark_all_as_read", {})
        self.assertEqual(self.get_mobile_push_notification_ids(user_profile),
                         [])
        mock_push_notifications.assert_called()

class GetUnreadMsgsTest(ZulipTestCase):
    def mute_stream(self, user_profile: UserProfile, stream: Stream) -> None:
        recipient = Recipient.objects.get(type_id=stream.id, type=Recipient.STREAM)
        subscription = Subscription.objects.get(
            user_profile=user_profile,
            recipient=recipient,
        )
        subscription.is_muted = True
        subscription.save()

    def mute_topic(self, user_profile: UserProfile, stream_name: str,
                   topic_name: str) -> None:
        realm = user_profile.realm
        stream = get_stream(stream_name, realm)
        recipient = stream.recipient

        add_topic_mute(
            user_profile=user_profile,
            stream_id=stream.id,
            recipient_id=recipient.id,
            topic_name=topic_name,
        )

    def test_raw_unread_stream(self) -> None:
        cordelia = self.example_user('cordelia')
        hamlet = self.example_user('hamlet')
        realm = hamlet.realm

        for stream_name in ['social', 'devel', 'test here']:
            self.subscribe(hamlet, stream_name)
            self.subscribe(cordelia, stream_name)

        all_message_ids: Set[int] = set()
        message_ids = dict()

        tups = [
            ('social', 'lunch'),
            ('test here', 'bla'),
            ('devel', 'python'),
            ('devel', 'ruby'),
        ]

        for stream_name, topic_name in tups:
            message_ids[topic_name] = [
                self.send_stream_message(
                    sender=cordelia,
                    stream_name=stream_name,
                    topic_name=topic_name,
                ) for i in range(3)
            ]
            all_message_ids |= set(message_ids[topic_name])

        self.assertEqual(len(all_message_ids), 12)  # sanity check on test setup

        self.mute_stream(
            user_profile=hamlet,
            stream=get_stream('test here', realm),
        )

        self.mute_topic(
            user_profile=hamlet,
            stream_name='devel',
            topic_name='ruby',
        )

        raw_unread_data = get_raw_unread_data(
            user_profile=hamlet,
        )

        stream_dict = raw_unread_data['stream_dict']

        self.assertEqual(
            set(stream_dict.keys()),
            all_message_ids,
        )

        self.assertEqual(
            raw_unread_data['unmuted_stream_msgs'],
            set(message_ids['python']) | set(message_ids['lunch']),
        )

        self.assertEqual(
            stream_dict[message_ids['lunch'][0]],
            dict(
                sender_id=cordelia.id,
                stream_id=get_stream('social', realm).id,
                topic='lunch',
            ),
        )

    def test_raw_unread_huddle(self) -> None:
        cordelia = self.example_user('cordelia')
        othello = self.example_user('othello')
        hamlet = self.example_user('hamlet')
        prospero = self.example_user('prospero')

        huddle1_message_ids = [
            self.send_huddle_message(
                cordelia,
                [hamlet, othello],
            )
            for i in range(3)
        ]

        huddle2_message_ids = [
            self.send_huddle_message(
                cordelia,
                [hamlet, prospero],
            )
            for i in range(3)
        ]

        raw_unread_data = get_raw_unread_data(
            user_profile=hamlet,
        )

        huddle_dict = raw_unread_data['huddle_dict']

        self.assertEqual(
            set(huddle_dict.keys()),
            set(huddle1_message_ids) | set(huddle2_message_ids),
        )

        huddle_string = ','.join(
            str(uid)
            for uid in sorted([cordelia.id, hamlet.id, othello.id])
        )

        self.assertEqual(
            huddle_dict[huddle1_message_ids[0]],
            dict(user_ids_string=huddle_string),
        )

    def test_raw_unread_personal(self) -> None:
        cordelia = self.example_user('cordelia')
        othello = self.example_user('othello')
        hamlet = self.example_user('hamlet')

        cordelia_pm_message_ids = [
            self.send_personal_message(cordelia, hamlet)
            for i in range(3)
        ]

        othello_pm_message_ids = [
            self.send_personal_message(othello, hamlet)
            for i in range(3)
        ]

        raw_unread_data = get_raw_unread_data(
            user_profile=hamlet,
        )

        pm_dict = raw_unread_data['pm_dict']

        self.assertEqual(
            set(pm_dict.keys()),
            set(cordelia_pm_message_ids) | set(othello_pm_message_ids),
        )

        self.assertEqual(
            pm_dict[cordelia_pm_message_ids[0]],
            dict(sender_id=cordelia.id),
        )

    def test_raw_unread_personal_from_self(self) -> None:
        hamlet = self.example_user('hamlet')

        def send_unread_pm(other_user: UserProfile) -> Message:
            # It is rare to send a message from Hamlet to Othello
            # (or any other user) and have it be unread for
            # Hamlet himself, but that is actually normal
            # behavior for most API clients.
            message_id = self.send_personal_message(
                from_user=hamlet,
                to_user=other_user,
                sending_client_name='some_api_program',
            )

            # Check our test setup is correct--the message should
            # not have looked like it was sent by a human.
            message = Message.objects.get(id=message_id)
            self.assertFalse(message.sent_by_human())

            # And since it was not sent by a human, it should not
            # be read, not even by the sender (Hamlet).
            um = UserMessage.objects.get(
                user_profile_id=hamlet.id,
                message_id=message_id,
            )
            self.assertFalse(um.flags.read)

            return message

        othello = self.example_user('othello')
        othello_msg = send_unread_pm(other_user=othello)

        # And now check the unread data structure...
        raw_unread_data = get_raw_unread_data(
            user_profile=hamlet,
        )

        pm_dict = raw_unread_data['pm_dict']

        self.assertEqual(set(pm_dict.keys()), {othello_msg.id})

        # For legacy reason we call the field `sender_id` here,
        # but it really refers to the other user id in the conversation,
        # which is Othello.
        self.assertEqual(
            pm_dict[othello_msg.id],
            dict(sender_id=othello.id),
        )

        cordelia = self.example_user('cordelia')
        cordelia_msg = send_unread_pm(other_user=cordelia)

        apply_unread_message_event(
            user_profile=hamlet,
            state=raw_unread_data,
            message=MessageDict.wide_dict(cordelia_msg),
            flags=[],
        )
        self.assertEqual(
            set(pm_dict.keys()),
            {othello_msg.id, cordelia_msg.id},
        )

        # Again, `sender_id` is misnamed here.
        self.assertEqual(
            pm_dict[cordelia_msg.id],
            dict(sender_id=cordelia.id),
        )

        # Send a message to ourself.
        hamlet_msg = send_unread_pm(other_user=hamlet)
        apply_unread_message_event(
            user_profile=hamlet,
            state=raw_unread_data,
            message=MessageDict.wide_dict(hamlet_msg),
            flags=[],
        )
        self.assertEqual(
            set(pm_dict.keys()),
            {othello_msg.id, cordelia_msg.id, hamlet_msg.id},
        )

        # Again, `sender_id` is misnamed here.
        self.assertEqual(
            pm_dict[hamlet_msg.id],
            dict(sender_id=hamlet.id),
        )

        # Call get_raw_unread_data again.
        raw_unread_data = get_raw_unread_data(
            user_profile=hamlet,
        )
        pm_dict = raw_unread_data['pm_dict']

        self.assertEqual(
            set(pm_dict.keys()),
            {othello_msg.id, cordelia_msg.id, hamlet_msg.id},
        )

        # Again, `sender_id` is misnamed here.
        self.assertEqual(
            pm_dict[hamlet_msg.id],
            dict(sender_id=hamlet.id),
        )

    def test_unread_msgs(self) -> None:
        sender = self.example_user('cordelia')
        sender_id = sender.id
        user_profile = self.example_user('hamlet')
        othello = self.example_user('othello')

        pm1_message_id = self.send_personal_message(sender, user_profile, "hello1")
        pm2_message_id = self.send_personal_message(sender, user_profile, "hello2")

        muted_stream = self.subscribe(user_profile, 'Muted Stream')
        self.mute_stream(user_profile, muted_stream)
        self.mute_topic(user_profile, 'Denmark', 'muted-topic')

        stream_message_id = self.send_stream_message(sender, "Denmark", "hello")
        muted_stream_message_id = self.send_stream_message(sender, "Muted Stream", "hello")
        muted_topic_message_id = self.send_stream_message(
            sender,
            "Denmark",
            topic_name="muted-topic",
            content="hello",
        )

        huddle_message_id = self.send_huddle_message(
            sender,
            [user_profile, othello],
            'hello3',
        )

        def get_unread_data() -> UnreadMessagesResult:
            raw_unread_data = get_raw_unread_data(user_profile)
            aggregated_data = aggregate_unread_data(raw_unread_data)
            return aggregated_data

        result = get_unread_data()

        # The count here reflects the count of unread messages that we will
        # report to users in the bankruptcy dialog, and for now it excludes unread messages
        # from muted treams, but it doesn't exclude unread messages from muted topics yet.
        self.assertEqual(result['count'], 4)

        unread_pm = result['pms'][0]
        self.assertEqual(unread_pm['sender_id'], sender_id)
        self.assertEqual(unread_pm['unread_message_ids'], [pm1_message_id, pm2_message_id])
        self.assertTrue('sender_ids' not in unread_pm)

        unread_stream = result['streams'][0]
        self.assertEqual(unread_stream['stream_id'], get_stream('Denmark', user_profile.realm).id)
        self.assertEqual(unread_stream['topic'], 'muted-topic')
        self.assertEqual(unread_stream['unread_message_ids'], [muted_topic_message_id])
        self.assertEqual(unread_stream['sender_ids'], [sender_id])

        unread_stream = result['streams'][1]
        self.assertEqual(unread_stream['stream_id'], get_stream('Denmark', user_profile.realm).id)
        self.assertEqual(unread_stream['topic'], 'test')
        self.assertEqual(unread_stream['unread_message_ids'], [stream_message_id])
        self.assertEqual(unread_stream['sender_ids'], [sender_id])

        unread_stream = result['streams'][2]
        self.assertEqual(unread_stream['stream_id'], get_stream('Muted Stream', user_profile.realm).id)
        self.assertEqual(unread_stream['topic'], 'test')
        self.assertEqual(unread_stream['unread_message_ids'], [muted_stream_message_id])
        self.assertEqual(unread_stream['sender_ids'], [sender_id])

        huddle_string = ','.join(str(uid) for uid in sorted([sender_id, user_profile.id, othello.id]))

        unread_huddle = result['huddles'][0]
        self.assertEqual(unread_huddle['user_ids_string'], huddle_string)
        self.assertEqual(unread_huddle['unread_message_ids'], [huddle_message_id])
        self.assertTrue('sender_ids' not in unread_huddle)

        self.assertEqual(result['mentions'], [])

        um = UserMessage.objects.get(
            user_profile_id=user_profile.id,
            message_id=stream_message_id,
        )
        um.flags |= UserMessage.flags.mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [stream_message_id])

        um.flags = UserMessage.flags.has_alert_word
        um.save()
        result = get_unread_data()
        # TODO: This should change when we make alert words work better.
        self.assertEqual(result['mentions'], [])

        um.flags = UserMessage.flags.wildcard_mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [stream_message_id])

        um.flags = 0
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        # Test with a muted stream
        um = UserMessage.objects.get(
            user_profile_id=user_profile.id,
            message_id=muted_stream_message_id,
        )
        um.flags = UserMessage.flags.mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [muted_stream_message_id])

        um.flags = UserMessage.flags.has_alert_word
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        um.flags = UserMessage.flags.wildcard_mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        um.flags = 0
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        # Test with a muted topic
        um = UserMessage.objects.get(
            user_profile_id=user_profile.id,
            message_id=muted_topic_message_id,
        )
        um.flags = UserMessage.flags.mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [muted_topic_message_id])

        um.flags = UserMessage.flags.has_alert_word
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        um.flags = UserMessage.flags.wildcard_mentioned
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])

        um.flags = 0
        um.save()
        result = get_unread_data()
        self.assertEqual(result['mentions'], [])
