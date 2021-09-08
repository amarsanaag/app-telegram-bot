import json
import logging
import os
import random
import uuid
from datetime import datetime
from typing import Optional, List

from ask_for_help_bot.pending_conversations import PendingQuestionToAnswer
from ask_for_help_bot.pending_messages_job import PendingMessagesJob
from chatbot_core.model.context import ConversationContext
from chatbot_core.model.details import TelegramDetails
from chatbot_core.model.event import IncomingSocialEvent
from chatbot_core.model.message import IncomingTextMessage
from chatbot_core.model.user_context import UserConversationContext
from chatbot_core.nlp.handler import NLPHandler
from chatbot_core.translator.translator import Translator
from chatbot_core.v3.connector.social_connector import SocialConnector
from chatbot_core.v3.connector.social_connectors.telegram_connector import TelegramSocialConnector
from chatbot_core.v3.handler.helpers.intent_manager import IntentFulfillerV3
from chatbot_core.v3.job.job_manager import JobManager
from chatbot_core.v3.logger.event_logger import LoggerConnector
from chatbot_core.v3.model.messages import TextualResponse, RapidAnswerResponse, TelegramRapidAnswerResponse, \
    UrlImageResponse, ResponseMessage, TelegramTextualResponse
from chatbot_core.v3.model.outgoing_event import OutgoingEvent, NotificationEvent
from common.button_payload import ButtonPayload
from common.wenet_event_handler import WenetEventHandler
from uhopper.utils.alert import AlertModule
from wenet.common.interface.exceptions import TaskCreationError, RefreshTokenExpiredError, TaskTransactionCreationError
from wenet.common.model.message.event import WeNetAuthenticationEvent
from wenet.common.model.message.message import TextualMessage, Message, QuestionToAnswerMessage, \
    AnsweredQuestionMessage, IncentiveMessage, IncentiveBadge, AnsweredPickedMessage
from wenet.common.model.task.task import Task, TaskGoal
from wenet.common.model.task.transaction import TaskTransaction
from wenet.common.model.user.user_profile import WeNetUserProfile

logger = logging.getLogger("uhopper.chatbot.wenet.askforhelp.chatbot")


class AskForHelpHandler(WenetEventHandler):
    """
    The class that manages the Ask For Help Wenet chatbot.

    This is a DFA (deterministic finite automata), where the next action is given either by the current state,
    the intent of the incoming event or both the two things.
    """
    # context keys
    CONTEXT_DESIRED_ANSWERER = "desired_answerer"
    CONTEXT_DESIRED_ANSWERER_REASON = "desired_answerer_reason"
    CONTEXT_SENSITIVE_QUESTION = "sensitive_question"
    CONTEXT_ANONYMOUS_QUESTION = "anonymous_question"
    CONTEXT_ASKED_QUESTION = "asked_question"
    CONTEXT_ANSWER_TO_QUESTION = "answer_to_question"
    CONTEXT_QUESTION_TO_ANSWER = "question_to_answer"
    CONTEXT_MESSAGE_TO_REPORT = "message_to_report"
    CONTEXT_REPORTING_IS_QUESTION = "reporting_is_question"
    CONTEXT_REPORTING_REASON = "reporting_reason"
    CONTEXT_ORIGINAL_QUESTION_REPORTING = "original_question_reporting"
    CONTEXT_PROPOSED_TASKS = "proposed_tasks"
    CONTEXT_PENDING_ANSWERS = "pending_answers"
    # all the recognize intents
    INTENT_QUESTION = '/question'
    INTENT_QUESTION_FIRST = '/question_first'
    INTENT_ASK_TO_DIFFERENT = "ask_to_different"
    INTENT_ASK_TO_SIMILAR = "ask_to_similar"
    INTENT_ASK_TO_ANYONE = "ask_to_anyone"
    INTENT_ASK_TO_NEARBY = "nearby"
    INTENT_ASK_TO_ANYWHERE = "anywhere"
    INTENT_ANSWER_ANONYMOUSLY = "answer_anonymously"
    INTENT_ANSWER_NOT_ANONYMOUSLY = "answer_not_anonymously"
    INTENT_SENSITIVE_QUESTION = "sensitive"
    INTENT_NOT_SENSITIVE_QUESTION = "not_sensitive"
    INTENT_ANONYMOUS_QUESTION = "anonymous"
    INTENT_NOT_ANONYMOUS_QUESTION = "not_anonymous"
    INTENT_ANSWER_QUESTION = "answer_question"
    INTENT_ANSWER_REMIND_LATER = "answer_remind_later"
    INTENT_ANSWER_NOT = "answer_not"
    INTENT_QUESTION_REPORT = "question_report"
    INTENT_REPORT_ABUSIVE = "abusive"
    INTENT_REPORT_SPAM = "spam"
    INTENT_ASK_MORE_ANSWERS = "ask_more_answers"
    INTENT_ANSWER_REPORT = "answer_report"
    INTENT_ANSWER = "/answer"
    INTENT_ANSWER_PICKED_QUESTION = "picked_answer"
    INTENT_BEST_ANSWER = "best_answer"
    INTENT_PROFILE = '/profile'
    # available states
    STATE_QUESTION_1 = "question_1"
    STATE_QUESTION_2 = "question_2"
    STATE_QUESTION_3 = "question_3"
    STATE_QUESTION_4 = "question_4"
    STATE_QUESTION_4_1 = "question_4_1"
    STATE_QUESTION_5 = "question_5"
    STATE_ANSWERING = "answer_2"
    STATE_ANSWERING_SENSITIVE = "answer_sensitive"
    STATE_ANSWERING_ANONYMOUSLY = "answer_anonymously"
    # transaction labels
    LABEL_ANSWER_TRANSACTION = "answerTransaction"
    LABEL_NOT_ANSWER_TRANSACTION = "notAnswerTransaction"
    LABEL_REPORT_QUESTION_TRANSACTION = "reportQuestionTransaction"
    LABEL_REPORT_ANSWER_TRANSACTION = "reportAnswerTransaction"
    LABEL_MORE_ANSWER_TRANSACTION = "moreAnswerTransaction"
    LABEL_BEST_ANSWER_TRANSACTION = "bestAnswerTransaction"
    # keys used in Redis cache
    CACHE_LOCALE = "locale-{}"
    FIRST_ANSWER = "first-answer-{}"

    def __init__(self, instance_namespace: str, bot_id: str, handler_id: str, telegram_id: str, wenet_backend_url: str,
                 wenet_hub_url: str, app_id: str, client_secret: str, redirect_url: str, wenet_authentication_url: str,
                 wenet_authentication_management_url: str, task_type_id: str, alert_module: AlertModule,
                 connector: SocialConnector, nlp_handler: Optional[NLPHandler], translator: Optional[Translator],
                 delay_between_messages_sec: Optional[int] = None, delay_between_text_sec: Optional[float] = None,
                 logger_connectors: Optional[List[LoggerConnector]] = None):
        super().__init__(instance_namespace, bot_id, handler_id, telegram_id, wenet_backend_url, wenet_hub_url, app_id,
                         client_secret, redirect_url, wenet_authentication_url, wenet_authentication_management_url,
                         task_type_id, alert_module, connector, nlp_handler, translator, delay_between_messages_sec,
                         delay_between_text_sec, logger_connectors)

        JobManager.instance().add_job(PendingMessagesJob("wenet_ask_for_help_pending_messages_job",
                                                         self._instance_namespace, self._connector, None))
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_QUESTION, self.action_question).with_rule(intent=self.INTENT_QUESTION)
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_QUESTION_FIRST, self.action_question)
                .with_rule(intent=self.INTENT_QUESTION_FIRST)
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.STATE_QUESTION_1, self.action_question_2)
                .with_rule(static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_1))
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ASK_TO_DIFFERENT, self.action_question_3).with_rule(
                intent=self.INTENT_ASK_TO_DIFFERENT,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_2)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ASK_TO_SIMILAR, self.action_question_3).with_rule(
                intent=self.INTENT_ASK_TO_SIMILAR,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_2)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ASK_TO_ANYONE, self.action_question_3).with_rule(
                intent=self.INTENT_ASK_TO_ANYONE,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_2)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.STATE_QUESTION_3, self.action_question_4)
                .with_rule(static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_3))
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_SENSITIVE_QUESTION, self.action_question_4_1).with_rule(
                intent=self.INTENT_SENSITIVE_QUESTION,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_NOT_SENSITIVE_QUESTION, self.action_question_5).with_rule(
                intent=self.INTENT_NOT_SENSITIVE_QUESTION,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ANONYMOUS_QUESTION, self.action_question_5).with_rule(
                intent=self.INTENT_ANONYMOUS_QUESTION,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4_1)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_NOT_ANONYMOUS_QUESTION, self.action_question_5).with_rule(
                intent=self.INTENT_NOT_ANONYMOUS_QUESTION,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4_1)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ASK_TO_NEARBY, self.action_question_6).with_rule(
                intent=self.INTENT_ASK_TO_NEARBY,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_5)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ASK_TO_ANYWHERE, self.action_question_6).with_rule(
                intent=self.INTENT_ASK_TO_ANYWHERE,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_5)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3("", self.action_answer_sensitive_question).with_rule(
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_SENSITIVE)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3("", self.action_answer_question_2).with_rule(
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ANSWER_ANONYMOUSLY, self.action_answer_question_anonymously).with_rule(
                intent=self.INTENT_ANSWER_ANONYMOUSLY,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_ANONYMOUSLY)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ANSWER_NOT_ANONYMOUSLY, self.action_answer_question_anonymously).with_rule(
                intent=self.INTENT_ANSWER_NOT_ANONYMOUSLY,
                static_context=(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_ANONYMOUSLY)
            )
        )
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3(self.INTENT_ANSWER, self.action_answer).with_rule(intent=self.INTENT_ANSWER)
        )
        # self.intent_manager.with_fulfiller(
        #     IntentFulfillerV3(self.INTENT_PROFILE, self.action_profile).with_rule(intent=self.INTENT_PROFILE)
        # )
        # keep this as the last one!
        self.intent_manager.with_fulfiller(
            IntentFulfillerV3("", self.handle_button_with_payload).with_rule(
                regex=self.INTENT_BUTTON_WITH_PAYLOAD.format("[A-Za-z0-9-]+"))
        )

    def _get_user_locale_from_wenet_id(self, wenet_user_id: str, context: Optional[ConversationContext] = None) -> str:
        if not context:
            user_accounts = self.get_user_accounts(wenet_user_id)
            if len(user_accounts) != 1:
                raise Exception(f"No context associated with Wenet user {wenet_user_id}")
            context = user_accounts[0].context
        cached_locale = self.cache.get(self.CACHE_LOCALE.format(wenet_user_id))
        if not cached_locale:
            service_api = self._get_service_api_interface_connector_from_context(context)
            user_object = service_api.get_user_profile(wenet_user_id)
            if not user_object:
                logger.info(f"Unable to retrieve user profile [{wenet_user_id}]")
                return 'en'
            locale = user_object.locale if user_object.locale else 'en'
            self.cache.cache({"locale": locale}, ttl=int(os.getenv("LOCALE_TTL", 86400)),
                             key=self.CACHE_LOCALE.format(wenet_user_id))
            return locale
        return cached_locale.get("locale", "en")

    def _get_user_locale_from_incoming_event(self, incoming_event: IncomingSocialEvent) -> str:
        wenet_user_id = incoming_event.context.get_static_state(self.CONTEXT_WENET_USER_ID, None)
        if not wenet_user_id:
            logger.info(f"Impossible to get user locale from incoming event. The Wenet user ID is not in the context")
            return 'en'
        return self._get_user_locale_from_wenet_id(wenet_user_id, incoming_event.context)

    def _get_help_and_info_message(self, locale: str) -> str:
        return self._translator.get_translation_instance(locale).with_text("info_text").translate()

    def action_info(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        response.with_message(TextualResponse(self._get_help_and_info_message(user_locale)))
        return response

    def action_error(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        message = self._translator.get_translation_instance(user_locale).with_text("error_text").translate()
        response.with_message(TextualResponse(message))
        return response

    def _clear_context(self, context: ConversationContext) -> ConversationContext:
        context_to_remove = [
            self.CONTEXT_CURRENT_STATE, self.CONTEXT_ASKED_QUESTION, self.CONTEXT_DESIRED_ANSWERER,
            self.CONTEXT_QUESTION_TO_ANSWER, self.CONTEXT_MESSAGE_TO_REPORT,
            self.CONTEXT_REPORTING_IS_QUESTION, self.CONTEXT_REPORTING_REASON, self.CONTEXT_ORIGINAL_QUESTION_REPORTING,
            self.CONTEXT_DESIRED_ANSWERER_REASON, self.CONTEXT_SENSITIVE_QUESTION, self.CONTEXT_ANONYMOUS_QUESTION]
        for context_key in context_to_remove:
            context.delete_static_state(context_key)
        return context

    def _is_doing_another_action(self, context: ConversationContext) -> bool:
        """
        Returns True if the user is in another action (e.g. inside the /question flow), False otherwise
        """
        statuses = [self.STATE_ANSWERING, self.STATE_ANSWERING_SENSITIVE, self.STATE_ANSWERING_ANONYMOUSLY,
                    self.STATE_QUESTION_1, self.STATE_QUESTION_2, self.STATE_QUESTION_3, self.STATE_QUESTION_4,
                    self.STATE_QUESTION_4_1, self.STATE_QUESTION_5]
        current_status = context.get_static_state(self.CONTEXT_CURRENT_STATE, "")
        return current_status in statuses

    def cancel_action(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        context = incoming_event.context
        self._clear_context(context)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        message = self._translator.get_translation_instance(user_locale).with_text("cancel_text").translate()
        response = OutgoingEvent(social_details=incoming_event.social_details)
        response.with_message(TextualResponse(message))
        response.with_context(context)
        return response

    def handle_help(self, message: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        user_locale = self._get_user_locale_from_incoming_event(message)
        response = OutgoingEvent(social_details=message.social_details)
        response.with_message(TextualResponse(self._get_help_and_info_message(user_locale)))
        return response

    def _get_start_messages(self, user_locale: str) -> List[ResponseMessage]:
        message_1 = self._translator.get_translation_instance(user_locale).with_text("start_text_1").translate()
        message_2 = self._translator.get_translation_instance(user_locale).with_text("start_text_2").translate()
        badges_message = self._translator.get_translation_instance(user_locale).with_text("badges_promo")\
            .with_substitution("base_url", self.wenet_hub_url)\
            .with_substitution("app_id", self.app_id)\
            .translate()
        message_3 = self._get_help_and_info_message(user_locale)
        button_text = self._translator.get_translation_instance(user_locale).with_text("start_button").translate()
        final_message_with_button = RapidAnswerResponse(TextualResponse(message_3))
        final_message_with_button.with_textual_option(button_text, self.INTENT_QUESTION_FIRST)
        return [
            TextualResponse(message_1),
            TextualResponse(message_2),
            TextualResponse(badges_message),
            final_message_with_button]

    def action_start(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(
            social_details=incoming_event.social_details,
            messages=self._get_start_messages(user_locale)
        )
        return response

    def _send_new_message_from_wenet_notification(self, user: UserConversationContext) -> None:
        """
        Clear the context and send a message to the user saying that a new message from wenet has come,
        and that the previous operation is lost
        """
        context = self._clear_context(user.context)
        locale = self._get_user_locale_from_wenet_id(context.get_static_state(self.CONTEXT_WENET_USER_ID), context)
        text = self._translator.get_translation_instance(locale).with_text("message_from_wenet").translate()
        self.send_notification(NotificationEvent(user.social_details, [TextualResponse(text)], context))

    def handle_wenet_textual_message(self, message: TextualMessage) -> NotificationEvent:
        """
        Handle all the incoming textual messages
        """
        user_accounts = self.get_user_accounts(message.receiver_id)
        if len(user_accounts) != 1:
            error_message = f"No context associated with Wenet user {message.receiver_id}"
            logger.error(error_message)
            raise ValueError(error_message)

        user_account = user_accounts[0]
        # in case the user was doing something else, the previous operation is cancelled
        if self._is_doing_another_action(user_account.context):
            self._send_new_message_from_wenet_notification(user_account)

        title = "" if message.title == "" else f"*{self.parse_text_with_markdown(message.title)}*\n"
        response = TelegramTextualResponse(f"{title}_{self.parse_text_with_markdown(message.text)}_")
        return NotificationEvent(user_account.social_details, [response], user_account.context)

    def handle_nearby_question(self, message: QuestionToAnswerMessage, user_object: WeNetUserProfile,
                               questioning_user: WeNetUserProfile, sensitive: bool, anonymous: bool) -> TelegramRapidAnswerResponse:
        # Translate the message that someone near has a question and insert the details of the question, treat differently sensitive questions
        message_string = self._translator.get_translation_instance(user_object.locale)
        if sensitive:
            message_string = message_string.with_text("answer_sensitive_message_nearby")
        else:
            message_string = message_string.with_text("answer_message_nearby")

        message_string = message_string.with_substitution("question", self.parse_text_with_markdown(message.question)) \
            .with_substitution("user", questioning_user.name.first if questioning_user.name.first and not anonymous else "Anonymous") \
            .translate()

        # we create ids of all buttons, to know which buttons invalidate when one of them is clicked
        button_ids = [str(uuid.uuid4()) for _ in range(3)]
        button_data = {
            "task_id": message.task_id,
            "question": message.question,
            "sensitive": sensitive,
            "username": questioning_user.name.first if questioning_user.name.first and not anonymous else "Anonymous",
            "related_buttons": button_ids,
        }
        response = TelegramRapidAnswerResponse(TextualResponse(message_string), row_displacement=[1, 1, 1])
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_QUESTION).to_repr(), key=button_ids[0])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_question_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[0]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_NOT).to_repr(), key=button_ids[1])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_not_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[1]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_QUESTION_REPORT).to_repr(), key=button_ids[2])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_report_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[2]))
        return response

    def handle_question(self, message: QuestionToAnswerMessage, user_object: WeNetUserProfile,
                        questioning_user: WeNetUserProfile, sensitive: bool, anonymous: bool) -> TelegramRapidAnswerResponse:
        # Translate the message that someone in the community has a question and insert the details of the question, treat differently sensitive questions
        message_string = self._translator.get_translation_instance(user_object.locale)
        if sensitive:
            message_string = message_string.with_text("answer_sensitive_message_0")
        else:
            message_string = message_string.with_text("answer_message_0")

        message_string = message_string.with_substitution("question", self.parse_text_with_markdown(message.question)) \
            .with_substitution("user", questioning_user.name.first if questioning_user.name.first and not anonymous else "Anonymous") \
            .translate()

        # we create ids of all buttons, to know which buttons invalidate when one of them is clicked
        button_ids = [str(uuid.uuid4()) for _ in range(4)]
        button_data = {
            "task_id": message.task_id,
            "question": message.question,
            "sensitive": sensitive,
            "username": questioning_user.name.first if questioning_user.name.first and not anonymous else "Anonymous",
            "related_buttons": button_ids,
        }
        response = TelegramRapidAnswerResponse(TextualResponse(message_string), row_displacement=[1, 1, 1, 1])
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_QUESTION).to_repr(), key=button_ids[0])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_question_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[0]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_REMIND_LATER).to_repr(), key=button_ids[1])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_remind_later_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[1]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_NOT).to_repr(), key=button_ids[2])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_not_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[2]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_QUESTION_REPORT).to_repr(), key=button_ids[3])
        response.with_textual_option(self._translator.get_translation_instance(user_object.locale).with_text("answer_report_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[3]))
        return response

    def handle_answered_question(self, message: AnsweredQuestionMessage, user_object: WeNetUserProfile, answerer_user: WeNetUserProfile, question_task: Task) -> TelegramRapidAnswerResponse:
        answer_text = message.answer
        question_text = self.parse_text_with_markdown(question_task.goal.name)
        answer_transaction = None
        for transaction in question_task.transactions:
            if transaction.id == message.transaction_id:
                answer_transaction = transaction
        # Translate the message that there is a new answer and insert the details of the question and answer
        message_string = self._translator.get_translation_instance(user_object.locale) \
            .with_text("new_answer_message") \
            .with_substitution("question", question_text) \
            .with_substitution("answer", self.parse_text_with_markdown(answer_text)) \
            .with_substitution("username", answerer_user.name.first if answerer_user.name.first and not answer_transaction.attributes.get("anonymous") else "Anonymous") \
            .translate()

        answer = TelegramRapidAnswerResponse(TextualResponse(message_string), row_displacement=[1, 1, 1])
        button_report_text = self._translator.get_translation_instance(user_object.locale).with_text("answer_report_button").translate()
        button_more_answers_text = self._translator.get_translation_instance(user_object.locale).with_text("more_answers_button").translate()
        button_best_answers_text = self._translator.get_translation_instance(user_object.locale).with_text("best_answers_button").translate()
        button_ids = [str(uuid.uuid4()) for _ in range(3)]
        button_data = {
            "transaction_id": message.transaction_id,
            "task_id": question_task.task_id,
            "related_buttons": button_ids,
        }
        self.cache.cache(ButtonPayload(button_data, self.INTENT_BEST_ANSWER).to_repr(), key=button_ids[0])
        answer.with_textual_option(button_best_answers_text, self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[0]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ASK_MORE_ANSWERS).to_repr(), key=button_ids[1])
        answer.with_textual_option(button_more_answers_text, self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[1]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_REPORT).to_repr(), key=button_ids[2])
        answer.with_textual_option(button_report_text, self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[2]))
        return answer

    def handle_answered_picked(self, user_object: WeNetUserProfile, question_task: Task) -> TextualResponse:
        # Translate the message that the answer to a question was picked as the best and insert the details of the question
        message_string = self._translator.get_translation_instance(user_object.locale) \
            .with_text("picked_best_answer") \
            .with_substitution("question", self.parse_text_with_markdown(question_task.goal.name)) \
            .translate()
        return TextualResponse(message_string)

    def handle_wenet_message(self, message: Message) -> NotificationEvent:
        # new question to answer, or a new answer to a question
        # incentive messages or badges
        user_accounts = self.get_user_accounts(message.receiver_id)
        if len(user_accounts) != 1:
            raise Exception(f"No context associated with Wenet user {message.receiver_id}")

        user_account = user_accounts[0]
        context = user_account.context
        # in case the user was doing something else, the previous operation is cancelled
        if self._is_doing_another_action(context):
            self._send_new_message_from_wenet_notification(user_account)

        service_api = self._get_service_api_interface_connector_from_context(context)
        try:
            user_object = service_api.get_user_profile(str(message.receiver_id))
            if isinstance(message, QuestionToAnswerMessage):
                # handle a new question to answer checking if the question is for nearby people
                questioning_user = service_api.get_user_profile(str(message.user_id))
                question_task = service_api.get_task(message.task_id)

                #  Extract sensitive and anonymous attributes
                sensitive = question_task.attributes.get("sensitive")
                anonymous = question_task.attributes.get("anonymous")

                if question_task.attributes.get("positionOfAnswerer") == self.INTENT_ASK_TO_NEARBY:
                    response = self.handle_nearby_question(message, user_object, questioning_user, sensitive, anonymous)
                else:
                    response = self.handle_question(message, user_object, questioning_user, sensitive, anonymous)
                return NotificationEvent(user_account.social_details, [response], context)
            elif isinstance(message, AnsweredQuestionMessage):
                # handle an answer to a question
                answerer_id = message.user_id
                answerer_user = service_api.get_user_profile(str(answerer_id))
                question_task = service_api.get_task(message.task_id)
                answer = self.handle_answered_question(message, user_object, answerer_user, question_task)
                self._interface_connector.update_user_context(UserConversationContext(  # TODO check if this update is useful or not
                    social_details=user_account.social_details,
                    context=context,
                    version=UserConversationContext.VERSION_V3
                ))
                return NotificationEvent(user_account.social_details, [answer], context)
            elif isinstance(message, AnsweredPickedMessage):
                # handle an answer picked for a question
                question_task = service_api.get_task(message.task_id)
                response = self.handle_answered_picked(user_object, question_task)
                return NotificationEvent(user_account.social_details, [response], context)
            elif isinstance(message, IncentiveMessage):
                # handle an incentive message
                answer = TextualResponse(message.content)
                return NotificationEvent(user_account.social_details, [answer], context)
            elif isinstance(message, IncentiveBadge):
                # handle an incentive badge
                answer = TextualResponse(message.message)
                image = UrlImageResponse(message.image_url)
                return NotificationEvent(user_account.social_details, [answer, image], context)
            else:
                logger.warning(f"Received unrecognized message of type {type(message)}: {message.to_repr()}")
                raise Exception(f"Received unrecognized message of type {type(message)}: {message.to_repr()}")
        except RefreshTokenExpiredError:
            logger.exception("Refresh token is not longer valid")
            notification_event = NotificationEvent(social_details=user_account.social_details)
            notification_event.with_message(
                TelegramTextualResponse(
                    f"Sorry, the login credential are no longer valid, please login again in order to continue to use the bot:\n "
                    f"{self.wenet_authentication_url}/login?client_id={self.app_id}&external_id={user_account.social_details.get_user_id()}",
                    parse_mode=None
                )
            )
            return notification_event

    def handle_button_with_payload(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        """
        Handle a button with a payload saved into redis
        """
        button_id = incoming_event.incoming_message.intent.value.split("--")[-1]
        raw_button_payload = self.cache.get(button_id)
        if raw_button_payload is None:
            response = OutgoingEvent(social_details=incoming_event.social_details)
            user_locale = self._get_user_locale_from_incoming_event(incoming_event)
            response.with_message(TextualResponse(
                self._translator.get_translation_instance(user_locale).with_text("expired_button_message").translate()))
            return response
        button_payload = ButtonPayload.from_repr(raw_button_payload)
        if "related_buttons" in button_payload.payload:
            # removing the button and all the related buttons from the cache
            for button_to_remove in button_payload.payload["related_buttons"]:
                self.cache.remove(button_to_remove)
        else:
            # in case the button is not related with any other buttons, just remove it from the cache
            self.cache.remove(button_id)

        if button_payload.intent == self.INTENT_ASK_MORE_ANSWERS:
            return self.action_more_answers(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_QUESTION_REPORT or button_payload.intent == self.INTENT_ANSWER_REPORT:
            return self.action_report_message(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_REPORT_ABUSIVE or button_payload.intent == self.INTENT_REPORT_SPAM:
            return self.action_report_message_1(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_BEST_ANSWER:
            return self.action_best_answer(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_ANSWER_NOT:
            return self.action_not_answer_question(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_ANSWER_QUESTION:
            return self.action_answer_question(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_ANSWER_REMIND_LATER:
            return self.action_answer_remind_later(incoming_event, button_payload)
        elif button_payload.intent == self.INTENT_ANSWER_PICKED_QUESTION:
            return self.action_answer_picked_question(incoming_event, button_payload)
        raise ValueError(f"No action associated with intent [{button_payload.intent}]")

    def handle_wenet_authentication_result(self, message: WeNetAuthenticationEvent) -> NotificationEvent:
        if not isinstance(self._connector, TelegramSocialConnector):
            raise Exception("Expected telegram social connector")

        social_details = TelegramDetails(int(message.external_id), int(message.external_id),
                                         self._connector.get_telegram_bot_id())
        try:
            self._save_wenet_and_telegram_user_id_to_context(message, social_details)
            context = self._interface_connector.get_user_context(social_details)
            messages = self._get_start_messages(self._get_user_locale_from_wenet_id(
                context.context.get_static_state(self.CONTEXT_WENET_USER_ID), context.context))
            return NotificationEvent(social_details=social_details, messages=messages)
        except Exception as e:
            logger.exception("Unable to complete the wenet login", exc_info=e)
            return NotificationEvent(social_details).with_message(
                TextualResponse("Unable to complete the WeNetAuthentication")
            )

    def action_question(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        Beginning of the /question command
        """
        context = incoming_event.context
        context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_1)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        preamble_message = None
        if intent == self.INTENT_QUESTION_FIRST:
            preamble_message = self._translator.get_translation_instance(user_locale).with_text("question_0").translate()
        message = self._translator.get_translation_instance(user_locale).with_text("question_1").translate()
        response = OutgoingEvent(social_details=incoming_event.social_details)
        if preamble_message:
            response.with_message(TextualResponse(preamble_message))
        response.with_message(TextualResponse(message))
        response.with_context(context)
        return response

    def action_question_2(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        """
        Either ask for the person that should answer the question, or tell the user to be more inclusive
        """
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        if isinstance(incoming_event.incoming_message, IncomingTextMessage):
            question = incoming_event.incoming_message.text
            context = incoming_event.context
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_2)
            context.with_static_state(self.CONTEXT_ASKED_QUESTION, question)
            message = self._translator.get_translation_instance(user_locale).with_text("question_2").translate()
            button_1_text = self._translator.get_translation_instance(user_locale).with_text("type_answer_1").translate()
            button_2_text = self._translator.get_translation_instance(user_locale).with_text("type_answer_2").translate()
            button_3_text = self._translator.get_translation_instance(user_locale).with_text("type_answer_3").translate()
            response_with_buttons = TelegramRapidAnswerResponse(TextualResponse(message), row_displacement=[1, 1, 1])
            response_with_buttons.with_textual_option(button_1_text, self.INTENT_ASK_TO_DIFFERENT)
            response_with_buttons.with_textual_option(button_2_text, self.INTENT_ASK_TO_SIMILAR)
            response_with_buttons.with_textual_option(button_3_text, self.INTENT_ASK_TO_ANYONE)
            response.with_message(response_with_buttons)
            response.with_context(context)
        else:
            error_message = self._translator.get_translation_instance(user_locale).with_text("question_is_not_text").translate()
            response.with_message(TextualResponse(error_message))
        return response

    def action_question_3(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        Save the type of desired answerer, and ask for some more details about her. The intent contains the desired answerer
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        context.with_static_state(self.CONTEXT_DESIRED_ANSWERER, intent)
        context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_3)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        response.with_context(context)
        message = self._translator.get_translation_instance(user_locale).with_text("specify_answerer").translate()
        response.with_message(TextualResponse(message))
        return response

    def action_question_4(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        """
        Save the why this type of desired answerer, and ask whether the question is sensitive or not
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        if isinstance(incoming_event.incoming_message, IncomingTextMessage):
            desired_answerer_reason = incoming_event.incoming_message.text
            context = incoming_event.context
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4)
            context.with_static_state(self.CONTEXT_DESIRED_ANSWERER_REASON, desired_answerer_reason)
            message = self._translator.get_translation_instance(user_locale).with_text("sensitive_question").translate()
            button_1_text = self._translator.get_translation_instance(user_locale).with_text("not_sensitive").translate()
            button_2_text = self._translator.get_translation_instance(user_locale).with_text("sensitive").translate()
            response_with_buttons = TelegramRapidAnswerResponse(TextualResponse(message), row_displacement=[1, 1])
            response_with_buttons.with_textual_option(button_1_text, self.INTENT_NOT_SENSITIVE_QUESTION)
            response_with_buttons.with_textual_option(button_2_text, self.INTENT_SENSITIVE_QUESTION)
            response.with_message(response_with_buttons)
            response.with_context(context)
        else:
            error_message = self._translator.get_translation_instance(user_locale).with_text("answerer_details_are_not_text").translate()
            response.with_message(TextualResponse(error_message))
        return response

    def action_question_4_1(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        Save whether the question is sensitive or not, and ask whether to ask the question anonymously or not
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        context = incoming_event.context
        context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_4_1)
        context.with_static_state(self.CONTEXT_SENSITIVE_QUESTION, intent)
        message = self._translator.get_translation_instance(user_locale).with_text("anonymous_question").translate()
        button_1_text = self._translator.get_translation_instance(user_locale).with_text("anonymous").translate()
        button_2_text = self._translator.get_translation_instance(user_locale).with_text("not_anonymous").translate()
        response_with_buttons = TelegramRapidAnswerResponse(TextualResponse(message), row_displacement=[1, 1])
        response_with_buttons.with_textual_option(button_1_text, self.INTENT_ANONYMOUS_QUESTION)
        response_with_buttons.with_textual_option(button_2_text, self.INTENT_NOT_ANONYMOUS_QUESTION)
        response.with_message(response_with_buttons)
        response.with_context(context)
        return response

    def action_question_5(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        Save whether the question is not sensitive or whether to ask the question anonymously or not, and ask where should be the person that should answer the question
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        context = incoming_event.context
        context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_QUESTION_5)
        if intent in [self.INTENT_ANONYMOUS_QUESTION,  self.INTENT_NOT_ANONYMOUS_QUESTION]:
            context.with_static_state(self.CONTEXT_ANONYMOUS_QUESTION, intent)
        else:
            context.with_static_state(self.CONTEXT_SENSITIVE_QUESTION, intent)
        message = self._translator.get_translation_instance(user_locale).with_text("specify_answerer_location").translate()
        button_1_text = self._translator.get_translation_instance(user_locale).with_text("location_answer_1").translate()
        button_2_text = self._translator.get_translation_instance(user_locale).with_text("location_answer_2").translate()
        response_with_buttons = TelegramRapidAnswerResponse(TextualResponse(message), row_displacement=[1, 1])
        response_with_buttons.with_textual_option(button_1_text, self.INTENT_ASK_TO_NEARBY)
        response_with_buttons.with_textual_option(button_2_text, self.INTENT_ASK_TO_ANYWHERE)
        response.with_message(response_with_buttons)
        response.with_context(context)
        return response

    def action_question_6(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        Conclude the /question flow, with a final message
        """
        if incoming_event.context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        context = incoming_event.context
        if not context.has_static_state(self.CONTEXT_ASKED_QUESTION) \
                or not context.has_static_state(self.CONTEXT_DESIRED_ANSWERER) \
                or not context.has_static_state(self.CONTEXT_DESIRED_ANSWERER_REASON) \
                or not context.has_static_state(self.CONTEXT_SENSITIVE_QUESTION):
            raise Exception(f"Expected {self.CONTEXT_ASKED_QUESTION}, {self.CONTEXT_DESIRED_ANSWERER}, "
                            f"{self.CONTEXT_DESIRED_ANSWERER_REASON} and {self.CONTEXT_SENSITIVE_QUESTION} in the context")
        wenet_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        question = context.get_static_state(self.CONTEXT_ASKED_QUESTION)
        desired_answerer = context.get_static_state(self.CONTEXT_DESIRED_ANSWERER)
        answerer_details = context.get_static_state(self.CONTEXT_DESIRED_ANSWERER_REASON)
        sensitive = context.get_static_state(self.CONTEXT_SENSITIVE_QUESTION)
        anonymous = context.get_static_state(self.CONTEXT_ANONYMOUS_QUESTION, self.INTENT_NOT_ANONYMOUS_QUESTION)
        attributes = {
            "kindOfAnswerer": desired_answerer,
            "answeredDetails": answerer_details,
            "sensitive": True if sensitive == self.INTENT_SENSITIVE_QUESTION else False,
            "anonymous": True if anonymous == self.INTENT_ANONYMOUS_QUESTION else False,
            "positionOfAnswerer": intent,
        }
        question_task = Task(
            None,
            int(datetime.now().timestamp()),
            int(datetime.now().timestamp()),
            str(self.task_type_id),
            str(wenet_id),
            self.app_id,
            None,
            TaskGoal(question, ""),
            [],
            attributes,
            None,
            []
        )
        try:
            service_api.create_task(question_task)
            logger.debug(f"User [{wenet_id}] asked a question. Task created successfully")
            message = self._translator.get_translation_instance(user_locale).with_text("question_final").translate()
            response.with_message(TextualResponse(message))
        except TaskCreationError as e:
            logger.error(f"The service API responded with code {e.http_status} and message {json.dumps(e.json_response)}")
            message = self._translator.get_translation_instance(user_locale).with_text("error_task_creation").translate()
            response.with_message(TextualResponse(message))
        finally:
            context.delete_static_state(self.CONTEXT_ASKED_QUESTION)
            context.delete_static_state(self.CONTEXT_DESIRED_ANSWERER)
            context.delete_static_state(self.CONTEXT_DESIRED_ANSWERER_REASON)
            context.delete_static_state(self.CONTEXT_SENSITIVE_QUESTION)
            context.delete_static_state(self.CONTEXT_ANONYMOUS_QUESTION)
            context.delete_static_state(self.CONTEXT_CURRENT_STATE)
            response.with_context(context)
        return response

    def is_first_answer(self, wenet_user_id: str) -> bool:
        """
        Use Redis to keep track of the fact that a Wenet user has already answered someone else's question.
        This piece of information is used to decide whether or not showing the conduct instructions
        """
        first_answer = self.cache.get(self.FIRST_ANSWER.format(wenet_user_id))
        if first_answer is None:
            self.cache.cache({"has_answered": True}, key=self.FIRST_ANSWER.format(wenet_user_id))
            return True
        return False

    def action_answer_question(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        """
        QuestionToAnswerMessage flow, when user click on the answer button
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        user_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        show_conduct_message = True
        if user_id:
            is_first_answer = self.is_first_answer(user_id)
            show_conduct_message = is_first_answer or random.randint(1, 10) <= 2
        context.with_static_state(self.CONTEXT_QUESTION_TO_ANSWER, button_payload.payload["task_id"])
        if button_payload.payload.get("sensitive"):
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_SENSITIVE)
        else:
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING)
        if button_payload.payload.get("sensitive"):
            message = self._translator.get_translation_instance(user_locale).with_text("answer_sensitive_question").translate()
        else:
            message = self._translator.get_translation_instance(user_locale).with_text("answer_question").translate()
        response = OutgoingEvent(social_details=incoming_event.social_details)
        response.with_context(context)
        response.with_message(TextualResponse(message))
        if show_conduct_message:
            response.with_message(TextualResponse(self._translator.get_translation_instance(user_locale).with_text("question_0").translate()))
        return response

    def action_answer_picked_question(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        """
        /answer flow, when the user picks an answer
        """
        if incoming_event.context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")

        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        context.with_static_state(self.CONTEXT_QUESTION_TO_ANSWER, button_payload.payload["task_id"])
        context.delete_static_state(self.CONTEXT_PROPOSED_TASKS)
        user_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        task = service_api.get_task(button_payload.payload["task_id"])
        if button_payload.payload.get("sensitive"):
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_SENSITIVE)
            message = self._translator.get_translation_instance(user_locale).with_text("you_are_answering_to_sensitive")\
                .with_substitution("question", self.parse_text_with_markdown(task.goal.name))\
                .translate()
        else:
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING)
            message = self._translator.get_translation_instance(user_locale).with_text("you_are_answering_to")\
                .with_substitution("question", self.parse_text_with_markdown(task.goal.name))\
                .translate()

        response = OutgoingEvent(social_details=incoming_event.social_details)
        response.with_context(context)
        response.with_message(TelegramTextualResponse(message))
        is_first_time = self.is_first_answer(user_id)
        if is_first_time:
            response.with_message(TextualResponse(self._translator.get_translation_instance(user_locale).with_text("question_0").translate()))
        return response

    def action_answer_sensitive_question(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        """
        QuestionToAnswerMessage flow, collect the user's answer and since it is a sensitive question ask if should be anonymous the answer
        """
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        if not context.has_static_state(self.CONTEXT_QUESTION_TO_ANSWER):
            error_message = "Illegal state, expected the question ID in the context, but it does not exist"
            logger.error(error_message)
            raise ValueError(error_message)

        response = OutgoingEvent(social_details=incoming_event.social_details)
        if isinstance(incoming_event.incoming_message, IncomingTextMessage):
            context.with_static_state(self.CONTEXT_CURRENT_STATE, self.STATE_ANSWERING_ANONYMOUSLY)
            context.with_static_state(self.CONTEXT_ANSWER_TO_QUESTION, incoming_event.incoming_message.text)
            message = self._translator.get_translation_instance(user_locale).with_text("answer_anonymously").translate()
            button_1_text = self._translator.get_translation_instance(user_locale).with_text("anonymous_answer_1").translate()
            button_2_text = self._translator.get_translation_instance(user_locale).with_text("anonymous_answer_2").translate()
            response_with_buttons = TelegramRapidAnswerResponse(TextualResponse(message), row_displacement=[1, 1])
            response_with_buttons.with_textual_option(button_1_text, self.INTENT_ANSWER_ANONYMOUSLY)
            response_with_buttons.with_textual_option(button_2_text, self.INTENT_ANSWER_NOT_ANONYMOUSLY)
            response.with_message(response_with_buttons)
            response.with_context(context)
        else:
            error_message = self._translator.get_translation_instance(user_locale).with_text("answerer_is_not_text").translate()
            response.with_message(TextualResponse(error_message))
        return response

    def action_answer_question_anonymously(self, incoming_event: IncomingSocialEvent, intent: str) -> OutgoingEvent:
        """
        QuestionToAnswerMessage flow, collect if the user's answer should be anonymous and thank her
        """
        if incoming_event.context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")

        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context

        if not context.has_static_state(self.CONTEXT_QUESTION_TO_ANSWER):
            error_message = "Illegal state, expected the question ID in the context, but it does not exist"
            logger.error(error_message)
            raise ValueError(error_message)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        question_id = context.get_static_state(self.CONTEXT_QUESTION_TO_ANSWER)
        answer = context.get_static_state(self.CONTEXT_ANSWER_TO_QUESTION)
        actioneer_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        try:
            transaction = TaskTransaction(None, question_id, self.LABEL_ANSWER_TRANSACTION, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, {"answer": answer, "anonymous": True if intent == self.INTENT_ANSWER_ANONYMOUSLY else False}, [])
            service_api.create_task_transaction(transaction)
            logger.info("Sent task transaction: %s" % str(transaction.to_repr()))
            if intent == self.INTENT_ANSWER_ANONYMOUSLY:
                message = self._translator.get_translation_instance(user_locale).with_text("answered_message_anonymously").translate()
            else:
                message = self._translator.get_translation_instance(user_locale).with_text("answered_message").translate()
            response.with_message(TextualResponse(message))
        except TaskTransactionCreationError as e:
            response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
            logger.error(
                "Error in the creation of the transaction for answering the task [%s]. The service API responded with code %d and message %s"
                % (question_id, e.http_status, json.dumps(e.json_response)))
        finally:
            context.delete_static_state(self.CONTEXT_QUESTION_TO_ANSWER)
            context.delete_static_state(self.CONTEXT_CURRENT_STATE)
            response.with_context(context)
        return response

    def action_answer_question_2(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        """
        QuestionToAnswerMessage flow, collect the user's answer and thank her
        """
        if incoming_event.context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")

        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context

        if not context.has_static_state(self.CONTEXT_QUESTION_TO_ANSWER):
            error_message = "Illegal state, expected the question ID in the context, but it does not exist"
            logger.error(error_message)
            raise ValueError(error_message)
        response = OutgoingEvent(social_details=incoming_event.social_details)
        if isinstance(incoming_event.incoming_message, IncomingTextMessage):
            question_id = context.get_static_state(self.CONTEXT_QUESTION_TO_ANSWER)
            answer = incoming_event.incoming_message.text
            actioneer_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
            try:
                transaction = TaskTransaction(None, question_id, self.LABEL_ANSWER_TRANSACTION, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, {"answer": answer, "anonymous": False}, [])
                service_api.create_task_transaction(transaction)
                logger.info("Sent task transaction: %s" % str(transaction.to_repr()))
                message = self._translator.get_translation_instance(user_locale).with_text("answered_message").translate()
                response.with_message(TextualResponse(message))
            except TaskTransactionCreationError as e:
                response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
                logger.error(
                    "Error in the creation of the transaction for answering the task [%s]. The service API responded with code %d and message %s"
                    % (question_id, e.http_status, json.dumps(e.json_response)))
            finally:
                context.delete_static_state(self.CONTEXT_QUESTION_TO_ANSWER)
                context.delete_static_state(self.CONTEXT_CURRENT_STATE)
                response.with_context(context)
        else:
            error_message = self._translator.get_translation_instance(user_locale).with_text("answerer_is_not_text").translate()
            response.with_message(TextualResponse(error_message))
        return response

    def action_not_answer_question(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        if incoming_event.context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")
        context = incoming_event.context
        question_id = button_payload.payload["task_id"]
        actioneer_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        try:
            transaction = TaskTransaction(None, question_id, self.LABEL_NOT_ANSWER_TRANSACTION, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, {}, [])
            service_api.create_task_transaction(transaction)
            message = self._translator.get_translation_instance(user_locale).with_text("not_answer_response").translate()
            response.with_message(TextualResponse(message))
        except TaskTransactionCreationError as e:
            response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
            logger.error(
                "Error in the creation of the transaction for not answering the task [%s]. The service API responded with code %d and message %s"
                % (question_id, e.http_status, json.dumps(e.json_response)))
        response.with_context(context)
        return response

    def action_answer_remind_later(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        message = self._translator.get_translation_instance(user_locale).with_text("answer_remind_later_message").translate()
        response.with_message(TextualResponse(message))
        pending_answers = context.get_static_state(self.CONTEXT_PENDING_ANSWERS, {})
        question_id = button_payload.payload["task_id"]

        # Recreating the message that someone in the community has a question and insert the details of the question, treat differently sensitive questions
        message_string = self._translator.get_translation_instance(user_locale)
        if button_payload.payload.get("sensitive"):
            message_string = message_string.with_text("answer_sensitive_message_0")
        else:
            message_string = message_string.with_text("answer_message_0")

        message_string = message_string.with_substitution("question", self.parse_text_with_markdown(button_payload.payload["question"])) \
            .with_substitution("user", button_payload.payload["username"]) \
            .translate()

        button_ids = [str(uuid.uuid4()) for _ in range(4)]
        button_data = {
            "task_id": question_id,
            "related_buttons": button_ids,
            "question": button_payload.payload["question"],
            "username": button_payload.payload["username"],
        }
        response_to_store = TelegramRapidAnswerResponse(TextualResponse(message_string), row_displacement=[1, 1, 1, 1])

        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_QUESTION).to_repr(), key=button_ids[0])
        response_to_store.with_textual_option(self._translator.get_translation_instance(user_locale).with_text("answer_question_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[0]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_REMIND_LATER).to_repr(), key=button_ids[1])
        response_to_store.with_textual_option(self._translator.get_translation_instance(user_locale).with_text("answer_remind_later_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[1]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_ANSWER_NOT).to_repr(), key=button_ids[2])
        response_to_store.with_textual_option(self._translator.get_translation_instance(user_locale).with_text("answer_not_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[2]))
        self.cache.cache(ButtonPayload(button_data, self.INTENT_QUESTION_REPORT).to_repr(), key=button_ids[3])
        response_to_store.with_textual_option(self._translator.get_translation_instance(user_locale).with_text("answer_report_button").translate(), self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[3]))
        pending_answer = PendingQuestionToAnswer(question_id, response_to_store, incoming_event.social_details, sent=datetime.now())
        pending_answers[question_id] = pending_answer.to_repr()
        context.with_static_state(self.CONTEXT_PENDING_ANSWERS, pending_answers)
        response.with_context(context)
        return response

    def action_report_message(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        """
        First step of reporting a single message (either a question or an answer).
        The payload must have the task id, and in case of reporting an answer it has also the transaction id
        """
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        message_text = self._translator.get_translation_instance(user_locale).with_text("why_reporting_message").translate()
        button_why_reporting_1_text = self._translator.get_translation_instance(user_locale).with_text("button_why_reporting_1_text").translate()
        button_why_reporting_2_text = self._translator.get_translation_instance(user_locale).with_text("button_why_reporting_2_text").translate()
        button_why_reporting_3_text = self._translator.get_translation_instance(user_locale).with_text("button_why_reporting_3_text").translate()
        message = TelegramRapidAnswerResponse(TextualResponse(message_text), row_displacement=[1, 1, 1])
        button_ids = [str(uuid.uuid4()) for _ in range(2)]
        payload = button_payload.payload
        payload.update({"related_buttons": button_ids})
        self.cache.cache(ButtonPayload(button_payload.payload, self.INTENT_REPORT_ABUSIVE).to_repr(), key=button_ids[0])
        self.cache.cache(ButtonPayload(button_payload.payload, self.INTENT_REPORT_SPAM).to_repr(), key=button_ids[1])
        message.with_textual_option(button_why_reporting_1_text, self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[0]))
        message.with_textual_option(button_why_reporting_2_text, self.INTENT_BUTTON_WITH_PAYLOAD.format(button_ids[1]))
        message.with_textual_option(button_why_reporting_3_text, self.INTENT_CANCEL)
        response.with_message(message)
        return response

    def action_report_message_1(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        """
        Second step of reporting a single message (either a question or an answer).
        A transaction is sent
        """
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        task_id = button_payload.payload["task_id"]
        transaction_id = button_payload.payload.get("transaction_id", None)
        service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        attributes = {
            "reason": button_payload.intent,
        }
        if transaction_id is None:
            transaction_label = self.LABEL_REPORT_QUESTION_TRANSACTION
        else:
            transaction_label = self.LABEL_REPORT_ANSWER_TRANSACTION
            attributes.update({"transactionId": transaction_id})
        actioneer_id = incoming_event.context.get_static_state(self.CONTEXT_WENET_USER_ID)
        try:
            transaction = TaskTransaction(None, task_id, transaction_label, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, attributes, [])
            service_api.create_task_transaction(transaction)
            logger.info("Sent task transaction: %s" % str(transaction.to_repr()))
            message = self._translator.get_translation_instance(user_locale).with_text(
                "report_final_message").translate()
            response.with_message(TextualResponse(message))
        except TaskTransactionCreationError as e:
            response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
            logger.error(
                "Error in the creation of the transaction for reporting the task [%s]. The service API responded with code %d and message %s"
                % (task_id, e.http_status, json.dumps(e.json_response)))
        return response

    def action_more_answers(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        if context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")
        task_id = button_payload.payload["task_id"]
        actioneer_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        try:
            transaction = TaskTransaction(None, task_id, self.LABEL_MORE_ANSWER_TRANSACTION, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, {}, [])
            service_api.create_task_transaction(transaction)
            logger.info("Sent task transaction: %s" % str(transaction.to_repr()))
            message = self._translator.get_translation_instance(user_locale).with_text("ask_more_answers_text").translate()
            response.with_message(TextualResponse(message))
        except TaskTransactionCreationError as e:
            response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
            logger.error(
                "Error in the creation of the transaction to ask more responses for the task [%s]. The service API responded with code %d and message %s"
                % (task_id, e.http_status, json.dumps(e.json_response)))
        finally:
            context.delete_static_state(self.CONTEXT_CURRENT_STATE)
            response.with_context(context)
        return response

    def action_best_answer(self, incoming_event: IncomingSocialEvent, button_payload: ButtonPayload) -> OutgoingEvent:
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        task_id = button_payload.payload["task_id"]
        transaction_id = button_payload.payload["transaction_id"]
        service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        attributes = {
            "transactionId": transaction_id
        }
        actioneer_id = incoming_event.context.get_static_state(self.CONTEXT_WENET_USER_ID)
        try:
            transaction = TaskTransaction(None, task_id, self.LABEL_BEST_ANSWER_TRANSACTION, int(datetime.now().timestamp()), int(datetime.now().timestamp()), actioneer_id, attributes, [])
            service_api.create_task_transaction(transaction)
            logger.info("Sent task transaction: %s" % str(transaction.to_repr()))
            message = self._translator.get_translation_instance(user_locale).with_text(
                "best_answer_final_message").translate()
            response.with_message(TextualResponse(message))
        except TaskTransactionCreationError as e:
            response.with_message(TextualResponse("I'm sorry, something went wrong, try again later"))
            logger.error(
                "Error in the creation of the transaction for reporting the task [%s]. The service API responded with code %d and message %s"
                % (task_id, e.http_status, json.dumps(e.json_response)))
        return response

    def action_answer(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        response = OutgoingEvent(social_details=incoming_event.social_details)
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        context = incoming_event.context
        if context is not None:
            service_api = self._get_service_api_interface_connector_from_context(incoming_event.context)
        else:
            raise Exception(f"Missing conversation context for event {incoming_event}")
        user_id = context.get_static_state(self.CONTEXT_WENET_USER_ID)
        tasks = [t for t in service_api.get_all_tasks_of_application(self.app_id)
                 if t.requester_id != user_id and user_id not in set(
                [transaction.actioneer_id for transaction in t.transactions if transaction.label == "answerTransaction"])]

        if not tasks:
            response.with_message(TextualResponse(
                self._translator.get_translation_instance(user_locale).with_text("answers_no_tasks").translate()))
        else:
            if len(tasks) > 3:
                # if more than 3 tasks, pick 3 random
                tasks = random.sample(tasks, k=3)
            text = self._translator.get_translation_instance(user_locale).with_text("answers_tasks_intro").translate()
            proposed_tasks = []
            tasks_texts = []
            for task in tasks:
                questioning_user = service_api.get_user_profile(str(task.requester_id))
                if questioning_user:
                    task_text = f"#{1 + len(proposed_tasks)}: *{self.parse_text_with_markdown(task.goal.name)}* - {questioning_user.name.first if questioning_user.name.first and not task.attributes.get('anonymous') else 'Anonymous'}"
                    if task.attributes.get('sensitive'):
                        task_text = task_text + f" - {self._translator.get_translation_instance(user_locale).with_text('sensitive').translate()}"
                    tasks_texts.append(task_text)
                    proposed_tasks.append(task)
            context.with_static_state(self.CONTEXT_PROPOSED_TASKS, [task.task_id for task in proposed_tasks])
            message_text = '\n'.join([text] + tasks_texts + [self._translator.get_translation_instance(user_locale).with_text("answers_tasks_choose").translate()])
            rapid_answer = RapidAnswerResponse(TextualResponse(message_text))
            for i in range(len(proposed_tasks)):
                button_id = self.cache.cache(ButtonPayload({"task_id": proposed_tasks[i].task_id, "sensitive": proposed_tasks[i].attributes.get('sensitive')}, self.INTENT_ANSWER_PICKED_QUESTION).to_repr())
                rapid_answer.with_textual_option(f"#{1 + i}", self.INTENT_BUTTON_WITH_PAYLOAD.format(button_id))
            response.with_message(rapid_answer)
        response.with_context(context)
        return response

    def action_profile(self, incoming_event: IncomingSocialEvent, _: str) -> OutgoingEvent:
        user_locale = self._get_user_locale_from_incoming_event(incoming_event)
        response = OutgoingEvent(incoming_event.social_details)
        text = self._translator.get_translation_instance(user_locale).with_text("not_implemented").translate()
        response.with_message(TextualResponse(text))
        return response
