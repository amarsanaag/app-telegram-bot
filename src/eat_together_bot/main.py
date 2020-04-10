import logging.config
import os

from eat_together_bot.handler import EatTogetherHandler
from log_config.logging_config import loggingConfiguration
from uhopper.utils.alert import AlertModule
from uhopper.utils.mqtt import MqttSubscriptionHandler
from chatbot_core.v3.connector.social_connectors.telegram_connector import TelegramSocialConnector
from chatbot_core.v3.handler.event_dipatcher import MultiThreadEventDispatcher
from chatbot_core.v3.handler.instance_manager import InstanceManager

logging.config.dictConfig(loggingConfiguration)
logger = logging.getLogger("uhopper.chatbot.wenet-eat-together-chatbot")

if __name__ == "__main__":
    topic = os.getenv("MQTT_TOPIC")
    subscriber = MqttSubscriptionHandler(os.getenv("MQTT_HOST"), os.getenv("MQTT_CLIENT_ID"), os.getenv("MQTT_USER"),
                                         os.getenv("MQTT_PASSWORD"))
    subscriber.add_subscription(topic)
    instance_namespace = os.getenv("INSTANCE_NAMESPACE")
    bot_token = os.getenv("TELEGRAM_KEY")
    connector = TelegramSocialConnector(bot_token)
    alert_module = AlertModule("wenet-eat-together-chatbot")
    alert_module.with_slack(["@nicolopomini"])
    handler = EatTogetherHandler(instance_namespace, "test_telegram", "test_telegram", alert_module, connector, None, None)
    instance_manager = InstanceManager(instance_namespace, subscriber, MultiThreadEventDispatcher())
    instance_manager.with_event_handler(handler)

    try:
        instance_manager.start()
    except KeyboardInterrupt:
        subscriber.disconnect()
