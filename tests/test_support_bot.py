import pytest

from support_bot import (
    TicketStore,
    find_faq_answer,
    message_targets_bot,
    normalize,
    parse_operator_ids,
    request_id_from_reply,
    remove_bot_mention,
    route_reply_request,
    telegram_reply_kwargs,
    route_incoming_request,
)


def test_normalize_and_exact_faq_match():
    assert normalize("  Какие часы работы?! ") == "какие часы работы"
    assert find_faq_answer("Какие часы работы?")
    assert find_faq_answer("Сколько стоит доставка?") is None


def test_ticket_lifecycle_and_one_active_ticket(tmp_path):
    store = TicketStore(str(tmp_path / "support.sqlite3"))
    first = store.create_or_get(10, "client", "Сложный вопрос")
    same = store.create_or_get(10, "client", "Дополнение")

    assert first.id == same.id
    assert store.active(10).id == first.id
    messages = store.connection.execute(
        "SELECT body FROM messages WHERE ticket_id=? ORDER BY id", (first.id,)
    ).fetchall()
    assert [row[0] for row in messages] == ["Сложный вопрос", "Дополнение"]

    assert store.close(first.id) is True
    assert store.active(10) is None
    assert store.close(first.id) is False


def test_operator_ids_are_numeric():
    assert parse_operator_ids("123, 456") == {123, 456}
    with pytest.raises(RuntimeError):
        parse_operator_ids("abc")


def test_group_messages_require_bot_mention():
    class Obj:
        pass

    update = Obj()
    update.effective_chat = Obj()
    update.effective_chat.type = "supergroup"
    update.message = Obj()
    update.message.text = "@support_bot Когда вы работаете?"

    assert message_targets_bot(update, "support_bot") is True
    assert message_targets_bot(update, "other_bot") is False
    assert remove_bot_mention(update.message.text, "support_bot") == "Когда вы работаете?"


def test_private_messages_do_not_require_mention():
    class Obj:
        pass

    update = Obj()
    update.effective_chat = Obj()
    update.effective_chat.type = "private"
    update.message = Obj()
    update.message.text = "Вопрос"

    assert message_targets_bot(update, "support_bot") is True


def test_operator_reply_extracts_request_id_from_notification():
    class Message:
        text = "Заявка №417\nКлиент: user"

    assert request_id_from_reply(Message()) == 417


def test_operator_reply_uses_original_telegram_message_and_topic():
    class Request:
        source_message_id = 902
        message_thread_id = 17

    assert telegram_reply_kwargs(Request()) == {
        "reply_to_message_id": 902,
        "message_thread_id": 17,
    }


def test_group_follow_up_from_another_user_appends_to_active_request():
    class Request:
        id = 55

    class Store:
        def __init__(self):
            self.added = []
            self.statuses = []
            self.created = []

        def get_active_for_message(self, user_id, chat_id, chat_type):
            assert (user_id, chat_id, chat_type) == (202, -1009, "supergroup")
            return Request()

        def add_user_message(self, *args):
            self.added.append(args)

        def create_request(self, *args, **kwargs):
            self.created.append((args, kwargs))
            raise AssertionError("a new request must not be created")

    store = Store()
    result = route_incoming_request(store, 202, "second-user", -1009, "supergroup", "дополнение", 88, 3)

    assert result.id == 55
    assert store.added == [(55, 202, "дополнение", 88, 3)]


def test_reply_to_tracked_bot_message_uses_existing_request_without_mention():
    class Message:
        request_id = 55
        author_role = "bot"

    class Request:
        id = 55
        status = "waiting_employee"

    class Store:
        def __init__(self):
            self.added = []

        def find_message_by_telegram(self, chat_id, message_id):
            assert (chat_id, message_id) == (-1009, 701)
            return Message()

        def get(self, request_id):
            assert request_id == 55
            return Request()

        def add_user_message(self, *args):
            self.added.append(args)

    store = Store()
    result = route_reply_request(store, 202, "client", -1009, "supergroup", "ответ", 701, 702, None)

    assert result.id == 55
    assert store.added == [(55, 202, "ответ", 702, None)]


def test_reply_to_tracked_message_of_closed_request_creates_new_request():
    class Message:
        request_id = 55
        author_role = "bot"

    class Request:
        id = 55
        status = "closed"

    class NewRequest:
        id = 56

    class Store:
        def find_message_by_telegram(self, chat_id, message_id):
            return Message()

        def get(self, request_id):
            return Request()

        def create_request(self, *args, **kwargs):
            assert args == (202, "client", -1009, "supergroup", "новый вопрос")
            assert kwargs == {"source_message_id": 702, "message_thread_id": 4}
            return NewRequest()

    store = Store()
    result = route_reply_request(store, 202, "client", -1009, "supergroup", "новый вопрос", 701, 702, 4)

    assert result.id == 56
