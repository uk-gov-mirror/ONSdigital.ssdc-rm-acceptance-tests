import json
import logging
import time
from datetime import datetime, timezone
from acceptance_tests.common.strtobool import strtobool

import requests
from behave import register_type
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from splinter import Browser
from structlog import wrap_logger

from acceptance_tests.utilities import iap_requests
from acceptance_tests.utilities.audit_trail_helper import log_out_user_context_values, parse_markdown_context_table
from acceptance_tests.utilities.eq_stub_helper import reset_eq_stub
from acceptance_tests.utilities.exception_manager_helper import get_bad_messages, \
    quarantine_bad_messages_check_and_reset
from acceptance_tests.utilities.iap_requests import get_support_frontend_headers
from acceptance_tests.utilities.notify_helper import reset_notify_stub
from acceptance_tests.utilities.parameter_parsers import parse_array_to_list, parse_json_object
from acceptance_tests.utilities.pubsub_helper import purge_outbound_topics_with_retry, purge_outbound_topics
from acceptance_tests.utilities.template_helper import create_email_template, create_export_file_template, \
    create_sms_template
from acceptance_tests.utilities.test_case_helper import test_helper
from config import Config

logger = wrap_logger(logging.getLogger(__name__))

register_type(boolean=strtobool)
register_type(json=parse_json_object)
register_type(array=parse_array_to_list)

CONTEXT_ATTRIBUTES = parse_markdown_context_table(Config.CODE_GUIDE_MARKDOWN_FILE_PATH)
TEMPLATE_FILES_PATH = Config.RESOURCE_FILE_PATH.joinpath('template_files')


def before_all(context):
    context.config.setup_logging()
    _setup_templates(context)

    purge_outbound_topics_with_retry()


def move_fulfilment_triggers_harmlessly_massively_into_the_future():
    # The year 3000 ought to be far enough in the future for this fulfilment to never trigger again, no?
    url = f'{Config.SUPPORT_TOOL_API_URL}/fulfilmentNextTriggers?triggerDateTime=3000-01-01T00:00:00.000Z'
    response = iap_requests.make_request(method='POST', url=url)
    response.raise_for_status()


def before_scenario(context, scenario):
    move_fulfilment_triggers_harmlessly_massively_into_the_future()

    context.test_start_utc_datetime = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)
    context.correlation_id = None
    context.originating_user = None
    context.sent_messages = []
    context.scenario_name = scenario

    if "reset_notify_stub" in scenario.tags:
        reset_notify_stub()

    if "reset_eq_stub" in scenario.tags:
        if "cloud_only" not in scenario.tags:
            logger.warning(
                'WARNING: Attempting to reset EQ stub on a scenario not tagged as "cloud_only", will likely fail')
        reset_eq_stub()

    if 'UI' in context.tags:
        service = Service()
        options = Options()
        options.add_argument("--verbose")
        context.browser = Browser('chrome', headless=Config.HEADLESS, service=service, options=options)

    if "SupportFrontend" in context.tags:
        if Config.SUPPORT_FRONTEND_IAP_CLIENT_ID:

            # TODO: Needs to be removed when sample files ATs work in GCP
            if scenario.name == "Upload a sample file":
                scenario.skip("Skipping - Sample file Scenarios aren't supported in GCP yet")

            headers = get_support_frontend_headers()
            context.browser.driver.execute_cdp_cmd(
                "Network.enable", {}
            )
            context.browser.driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders", headers
            )


def after_all(_context):
    move_fulfilment_triggers_harmlessly_massively_into_the_future()


def after_scenario(context, scenario):
    try:
        if getattr(scenario, 'status') == 'failed':
            log_out_user_context_values(context, CONTEXT_ATTRIBUTES)

        unexpected_bad_messages = get_bad_messages()

        if unexpected_bad_messages:
            _record_and_remove_any_unexpected_bad_messages(unexpected_bad_messages)
    finally:
        if 'UI' in context.tags and getattr(context, 'browser', None):
            try:
                context.browser.quit()
            except Exception:
                logger.exception('Failed to quit browser during after_scenario teardown')

    if "reset_eq_stub" in scenario.tags:
        reset_eq_stub()

    leftover_messages = purge_outbound_topics()
    if leftover_messages:
        test_helper.fail(f'There are left over messages on the following subscriptions: {leftover_messages}, see logs '
                         f'above for details.')


def _record_and_remove_any_unexpected_bad_messages(unexpected_bad_messages):
    logger.error('Unexpected exception(s) -- these could be due to an underpowered environment',
                 exception_manager_response=unexpected_bad_messages)

    requests.get(f'{Config.EXCEPTION_MANAGER_URL}/reset')
    time.sleep(25)  # 25 seconds should be long enough for error to happen again if it hasn't cleared itself

    list_of_bad_message_hashes = get_bad_messages()

    if list_of_bad_message_hashes:
        bad_message_details = []

        for bad_message_hash in list_of_bad_message_hashes:
            response = requests.get(f'{Config.EXCEPTION_MANAGER_URL}/badmessage/{bad_message_hash}')
            response.raise_for_status()
            bad_message_details.append(response.json())

        _clear_queues_for_bad_messages_and_reset_exception_manager(list_of_bad_message_hashes)
        logger.error('Unexpected exception(s) which were not due to eventual consistency timing',
                     exception_manager_response=bad_message_details)

        test_helper.fail(f'Unexpected exception(s) thrown by RM. Details: {bad_message_details}')


def _clear_queues_for_bad_messages_and_reset_exception_manager(list_of_bad_message_hashes):
    purge_outbound_topics_with_retry()

    quarantine_bad_messages_check_and_reset(list_of_bad_message_hashes)


def _setup_templates(context):
    email_templates_path = TEMPLATE_FILES_PATH.joinpath("email_templates.json")
    email_templates = json.loads(email_templates_path.read_text())
    context.email_templates = {template['templateName']: template for template in email_templates}
    context.email_packcodes = {}
    for template_name, template in context.email_templates.items():
        pack_code, notify_template_id = create_email_template(template['template'], template_name)
        context.email_packcodes[template['templateName']] = {"pack_code": pack_code,
                                                             "notify_template_id": notify_template_id}

    export_file_templates_path = TEMPLATE_FILES_PATH.joinpath("export_file_templates.json")
    export_file_templates = json.loads(export_file_templates_path.read_text())
    context.export_file_templates = {template['templateName']: template for template in export_file_templates}
    context.export_file_packcodes = {}
    for _, template in context.export_file_templates.items():
        pack_code = create_export_file_template(template['template'])
        context.export_file_packcodes[template['templateName']] = {"pack_code": pack_code}

    sms_templates_path = TEMPLATE_FILES_PATH.joinpath("sms_templates.json")
    sms_templates = json.loads(sms_templates_path.read_text())
    context.sms_templates = {template['templateName']: template for template in sms_templates}
    context.sms_packcodes = {}
    for _, template in context.sms_templates.items():
        pack_code, notify_template_id = create_sms_template(template['template'])
        context.sms_packcodes[template['templateName']] = {"pack_code": pack_code,
                                                           "notify_template_id": notify_template_id}
