"""
Entrypoint for streamlit, see https://docs.streamlit.io/
"""

import asyncio
import base64
import os
import subprocess
import traceback
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import PosixPath
from typing import cast
from pathlib import Path
from io import StringIO
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import httpx
import streamlit as st
from anthropic import RateLimitError
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaTextBlockParam,
    BetaToolResultBlockParam,
)
from streamlit.delta_generator import DeltaGenerator

from computer_use_demo.loop import (
    PROVIDER_TO_DEFAULT_MODEL_NAME,
    APIProvider,
    sampling_loop,
    running_test_cases,
    save_test_case,
)

from computer_use_demo.run_commands import run_commands
from computer_use_demo.tools import ToolResult

LOCAL_MOUNT_PATH = "/home/docker_mount"
CONFIG_DIR = PosixPath("~/.anthropic").expanduser()
API_KEY_FILE = CONFIG_DIR / "api_key"
STREAMLIT_STYLE = """
<style>
    /* Highlight the stop button in red */
    button[kind=header] {
        background-color: rgb(13, 50, 83);
        border: 1px solid rgb(13, 50, 83);
        color: rgb(255, 255, 255);
    }
     /* Hide the streamlit deploy button */
    .stAppDeployButton {
        visibility: hidden;
    }
</style>
"""

WARNING_TEXT = "⚠️ Security Alert: Never provide access to sensitive accounts or data, as malicious web content can hijack Claude's behavior"
INTERRUPT_TEXT = "(user stopped or interrupted and wrote the following)"
INTERRUPT_TOOL_ERROR = "human stopped or interrupted tool execution"
TEST_CASE_FOLDER = Path(os.path.join(LOCAL_MOUNT_PATH, 'test_cases'))
TEST_RESULT_FOLDER = Path(os.path.join(LOCAL_MOUNT_PATH, 'results'))


class Sender(StrEnum):
    USER = "user"
    BOT = "assistant"
    TOOL = "tool"


def setup_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "api_key" not in st.session_state:
        # Try to load API key from file first, then environment
        st.session_state.api_key = load_from_storage("api_key") or os.getenv(
            "ANTHROPIC_API_KEY", ""
        )
    if "provider" not in st.session_state:
        st.session_state.provider = (
            os.getenv("API_PROVIDER", "anthropic") or APIProvider.ANTHROPIC
        )
    if "provider_radio" not in st.session_state:
        st.session_state.provider_radio = st.session_state.provider
    if "model" not in st.session_state:
        _reset_model()
    if "auth_validated" not in st.session_state:
        st.session_state.auth_validated = False
    if "responses" not in st.session_state:
        st.session_state.responses = {}
    if "tools" not in st.session_state:
        st.session_state.tools = {}
    if "only_n_most_recent_images" not in st.session_state:
        st.session_state.only_n_most_recent_images = 3
    if "custom_system_prompt" not in st.session_state:
        st.session_state.custom_system_prompt = load_from_storage("system_prompt") or ""
    if "hide_images" not in st.session_state:
        st.session_state.hide_images = False
    if "in_sampling_loop" not in st.session_state:
        st.session_state.in_sampling_loop = False
    if "test_cases_uploaded" not in st.session_state:
        st.session_state.test_cases_uploaded = False
    if "save_test_expanded" not in st.session_state:
        st.session_state.save_test_expanded = False


def _reset_model():
    st.session_state.model = PROVIDER_TO_DEFAULT_MODEL_NAME[
        cast(APIProvider, st.session_state.provider)
    ]


async def main():
    """Render loop for streamlit"""
    setup_state()

    LOGO_URL_LARGE = Path(os.path.join(os.getcwd(), 'static_content', 'logo.svg'))
    LOGO_URL_SMALL = Path(os.path.join(os.getcwd(), 'static_content', 'logo_icon.png'))

    st.set_page_config(
        page_title="TestAid",
        page_icon=LOGO_URL_SMALL,
        menu_items={
            'Get Help': 'https://www.incubyte.co/contact-us',
            'About': "### Automate UI Testing\n\n This tool is designed to help you automate UI testing. It uses the Anthropic API to generate test cases based on your input.\n\nMade by Incubyte.\n\n",
        }
    )

    st.markdown(STREAMLIT_STYLE, unsafe_allow_html=True)

    st.logo(LOGO_URL_LARGE, link="https://www.incubyte.co/", icon_image=LOGO_URL_SMALL,)

    st.title("Automate UI Testing")

    chat, test_run, http_logs = st.tabs(["Create Test Case", "Run Test Cases", "HTTP Exchange Logs"])

    with st.sidebar:
    
        if st.button("Clear Chat", type="primary"):
            await _reset_chat()
    
        # Section 1: Setup Changes
        with st.expander("⚙️ Setup", expanded=False):
            def _reset_api_provider():
                if st.session_state.provider_radio != st.session_state.provider:
                    _reset_model()
                    st.session_state.provider = st.session_state.provider_radio
                    st.session_state.auth_validated = False
                    
            provider_options = [option.value for option in APIProvider]
            st.radio(
                "API Provider",
                options=provider_options,
                key="provider_radio", 
                format_func=lambda x: x.title(),
                on_change=_reset_api_provider,
            )

            st.text_input("Model", key="model")

            if st.session_state.provider == APIProvider.ANTHROPIC:
                st.text_input(
                    "Anthropic API Key",
                    type="password",
                    key="api_key",
                    on_change=lambda: save_to_storage("api_key", st.session_state.api_key),
                )

            st.number_input(
                "Only send N most recent images",
                min_value=0,
                key="only_n_most_recent_images", 
                help="To decrease total tokens sent, remove older screenshots"
            )

            st.text_area(
                "Custom System Prompt Suffix",
                key="custom_system_prompt",
                help="Additional instructions to append to system prompt",
                on_change=lambda: save_to_storage("system_prompt", st.session_state.custom_system_prompt),
            )

            st.checkbox("Hide screenshots", key="hide_images")

            if st.button("Reset Settings", type="primary"):
                await _reset_chat()


        # Section 3: Upload Test Cases
        with st.expander("▶️ Upload Test Cases", expanded=False):
            test_cases = st.file_uploader("Upload Test Cases",  type=["json"], accept_multiple_files=True)
            test_case_number = 1

            for test_case in test_cases:
                # To convert to a string based IO:
                stringio = StringIO(test_case.getvalue().decode("utf-8"))

                # To read file as string:
                string_data = stringio.read()

                test_file_name = f"TC_{test_case_number}_{test_case.name}"

                ## write test case to a local file with name as test_case_number
                with open(TEST_CASE_FOLDER / test_file_name, "w") as f:
                    f.write(string_data)

                test_case_number += 1

        with st.expander("💾 Save Test Cases", expanded=st.session_state.save_test_expanded):
            _render_save_button(st.session_state.messages)
            
             
    if not st.session_state.auth_validated:
        if auth_error := validate_auth(
            st.session_state.provider, st.session_state.api_key
        ):
            st.warning(f"Please resolve the following auth issue:\n\n{auth_error}")
            return
        else:
            st.session_state.auth_validated = True

    new_message = st.chat_input(
        "Type a UI test case to run..."
    )

    with test_run:
        ## get list of all the files in the test case folder and display them as table with a Run Button
        test_cases = os.listdir(TEST_CASE_FOLDER)
        test_cases = [test_case for test_case in test_cases if test_case.endswith('.json') and not test_case.endswith('_dialogue.json')]
        test_cases.sort()
        test_cases.insert(0, "Select All")
        
        ## display the test cases in a table
        test_cases_to_run = st.multiselect("Select Test Cases to Run", test_cases)
        
        if "Select All" in test_cases_to_run:
            test_cases_to_run = test_cases[1:]

        test_case_results = []

        if st.button("Run Test Cases", type="primary"):
            for test_case_to_run in test_cases_to_run:

                test_case_file = TEST_CASE_FOLDER / test_case_to_run
                with open(test_case_file, "r") as f:
                    test_case = json.load(f)

                st.session_state.messages = await _run_test_cases(test_case, http_logs)

                test_case_result_message = st.session_state.messages[-1].get("content")[0].get("text")
                result = "Pass" if "PASSED" in test_case_result_message else "Fail"

                ## store the test case results as a dict with test case name and results
                test_case_results.append({"Name": test_case_to_run, "Result": result  ,"Message": test_case_result_message})

        if test_case_results:
            ## create a filename which has date and timestamp
            test_result_file = f"test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            
            ## write the test case result to a file
            with open(TEST_RESULT_FOLDER / test_result_file, "w") as f:
                json.dump(test_case_results, f)
            
            ## write the test case result to a csv file
            with open(TEST_RESULT_FOLDER / test_result_file.replace('.json', 'csv'), "w") as f:
                f.write("Name,Result,Message\n")
                for test_case_result in test_case_results:
                    f.write(f"{test_case_result.get('Name')},{test_case_result.get('Result')},{test_case_result.get('Message')}\n")

            ## display the test case results as table   
            st.table(test_case_results)

    with chat:
        # render past chats
        for message in st.session_state.messages:
            if isinstance(message["content"], str):
                _render_message(message["role"], message["content"])
            elif isinstance(message["content"], list):
                for block in message["content"]:
                    # the tool result we send back to the Anthropic API isn't sufficient to render all details,
                    # so we store the tool use responses
                    if isinstance(block, dict) and block["type"] == "tool_result":
                        _render_message(
                            Sender.TOOL, st.session_state.tools[block["tool_use_id"]]
                        )
                    else:
                        _render_message(
                            message["role"],
                            cast(BetaContentBlockParam | ToolResult, block),
                        )

        # render past http exchanges
        for identity, (request, response) in st.session_state.responses.items():
            _render_api_response(request, response, identity, http_logs)

        # render past chats
        if new_message:
            st.session_state.messages.append(
                {
                    "role": Sender.USER,
                    "content": [
                        *maybe_add_interruption_blocks(),
                        BetaTextBlockParam(type="text", text=new_message),
                    ],
                }
            )
            _render_message(Sender.USER, new_message)

        try:
            most_recent_message = st.session_state["messages"][-1]
        except IndexError:
            return

        if most_recent_message["role"] is not Sender.USER:
            # we don't have a user message to respond to, exit early
            return

        st.session_state.messages = await _run_agent_sampling_loop(st.session_state.messages, http_logs)

        st.session_state.save_test_expanded = True



def maybe_add_interruption_blocks():
    if not st.session_state.in_sampling_loop:
        return []
    # If this function is called while we're in the sampling loop, we can assume that the previous sampling loop was interrupted
    # and we should annotate the conversation with additional context for the model and heal any incomplete tool use calls
    result = []
    last_message = st.session_state.messages[-1]
    previous_tool_use_ids = [
        block["id"] for block in last_message["content"] if block["type"] == "tool_use"
    ]
    for tool_use_id in previous_tool_use_ids:
        st.session_state.tools[tool_use_id] = ToolResult(error=INTERRUPT_TOOL_ERROR)
        result.append(
            BetaToolResultBlockParam(
                tool_use_id=tool_use_id,
                type="tool_result",
                content=INTERRUPT_TOOL_ERROR,
                is_error=True,
            )
        )
    result.append(BetaTextBlockParam(type="text", text=INTERRUPT_TEXT))
    return result


@contextmanager
def track_sampling_loop():
    st.session_state.in_sampling_loop = True
    yield
    st.session_state.in_sampling_loop = False


def validate_auth(provider: APIProvider, api_key: str | None):
    if provider == APIProvider.ANTHROPIC:
        if not api_key:
            return "Enter your Anthropic API key in the sidebar to continue."
    if provider == APIProvider.BEDROCK:
        import boto3

        if not boto3.Session().get_credentials():
            return "You must have AWS credentials set up to use the Bedrock API."
    if provider == APIProvider.VERTEX:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        if not os.environ.get("CLOUD_ML_REGION"):
            return "Set the CLOUD_ML_REGION environment variable to use the Vertex API."
        try:
            google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        except DefaultCredentialsError:
            return "Your google cloud credentials are not set up correctly."


def load_from_storage(filename: str) -> str | None:
    """Load data from a file in the storage directory."""
    try:
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            data = file_path.read_text().strip()
            if data:
                return data
    except Exception as e:
        st.write(f"Debug: Error loading {filename}: {e}")
    return None


def save_to_storage(filename: str, data: str) -> None:
    """Save data to a file in the storage directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = CONFIG_DIR / filename
        file_path.write_text(data)
        # Ensure only user can read/write the file
        file_path.chmod(0o600)
    except Exception as e:
        st.write(f"Debug: Error saving {filename}: {e}")


def _api_response_callback(
    request: httpx.Request,
    response: httpx.Response | object | None,
    error: Exception | None,
    tab: DeltaGenerator,
    response_state: dict[str, tuple[httpx.Request, httpx.Response | object | None]],
):
    """
    Handle an API response by storing it to state and rendering it.
    """
    response_id = datetime.now().isoformat()
    response_state[response_id] = (request, response)
    if error:
        _render_error(error)
    _render_api_response(request, response, response_id, tab)


def _tool_output_callback(
    tool_output: ToolResult, tool_id: str, tool_state: dict[str, ToolResult]
):
    """Handle a tool output by storing it to state and rendering it."""
    tool_state[tool_id] = tool_output
    _render_message(Sender.TOOL, tool_output)


def _render_api_response(
    request: httpx.Request,
    response: httpx.Response | object | None,
    response_id: str,
    tab: DeltaGenerator,
):
    """Render an API response to a streamlit tab"""
    with tab:
        with st.expander(f"Request/Response ({response_id})"):
            newline = "\n\n"
            st.markdown(
                f"`{request.method} {request.url}`{newline}{newline.join(f'`{k}: {v}`' for k, v in request.headers.items())}"
            )
            st.json(request.read().decode())
            st.markdown("---")
            if isinstance(response, httpx.Response):
                st.markdown(
                    f"`{response.status_code}`{newline}{newline.join(f'`{k}: {v}`' for k, v in response.headers.items())}"
                )
                st.json(response.text)
            else:
                st.write(response)


def _render_error(error: Exception):
    if isinstance(error, RateLimitError):
        body = "You have been rate limited."
        if retry_after := error.response.headers.get("retry-after"):
            body += f" **Retry after {str(timedelta(seconds=int(retry_after)))} (HH:MM:SS).** See our API [documentation](https://docs.anthropic.com/en/api/rate-limits) for more details."
        body += f"\n\n{error.message}"
    else:
        body = str(error)
        body += "\n\n**Traceback:**"
        lines = "\n".join(traceback.format_exception(error))
        body += f"\n\n```{lines}```"
    save_to_storage(f"error_{datetime.now().timestamp()}.md", body)
    st.error(f"**{error.__class__.__name__}**\n\n{body}", icon=":material/error:")


def _render_message(
    sender: Sender,
    message: str | BetaContentBlockParam | ToolResult,
):
    """Convert input from the user or output from the agent to a streamlit message."""
    # streamlit's hotreloading breaks isinstance checks, so we need to check for class names
    is_tool_result = not isinstance(message, str | dict)
    if not message or (
        is_tool_result
        and st.session_state.hide_images
        and not hasattr(message, "error")
        and not hasattr(message, "output")
    ):
        return
    with st.chat_message(sender):
        if is_tool_result:
            message = cast(ToolResult, message)
            if message.output:
                if message.__class__.__name__ == "CLIResult":
                    st.code(message.output)
                else:
                    st.markdown(message.output)
            if message.error:
                st.error(message.error)
            if message.base64_image and not st.session_state.hide_images:
                st.image(base64.b64decode(message.base64_image))
        elif isinstance(message, dict):
            if message["type"] == "text":
                st.write(message["text"])
            elif message["type"] == "tool_use":
                st.code(f'Tool Use: {message["name"]}\nInput: {message["input"]}')
            else:
                # only expected return types are text and tool_use
                raise Exception(f'Unexpected response type {message["type"]}')
        else:
            st.markdown(message)


def _render_download_button(
    data:str
):
   st.download_button(label='💾 Save Test Case', data=data, mime='application/json')


async def _run_agent_sampling_loop(messages, http_logs):
    with track_sampling_loop():
        # run the agent sampling loop with the newest message
        messages = await sampling_loop(
            system_prompt_suffix=st.session_state.custom_system_prompt,
            model=st.session_state.model,
            provider=st.session_state.provider,
            messages=messages,
            output_callback=partial(_render_message, Sender.BOT),
            tool_output_callback=partial(
                _tool_output_callback, tool_state=st.session_state.tools
            ),
            api_response_callback=partial(
                _api_response_callback,
                tab=http_logs,
                response_state=st.session_state.responses,
            ),
            api_key=st.session_state.api_key,
            only_n_most_recent_images=st.session_state.only_n_most_recent_images,
        )
    
        return messages


async def _run_test_cases(messages, http_logs):
    with track_sampling_loop():
        # logger.info(f"Running test cases {messages}")
        # run the agent sampling loop with the newest message
        messages = await running_test_cases(
            system_prompt_suffix=st.session_state.custom_system_prompt,
            model=st.session_state.model,
            provider=st.session_state.provider,
            messages=messages,
            output_callback=partial(_render_message, Sender.BOT),
            tool_output_callback=partial(
                _tool_output_callback, tool_state=st.session_state.tools
            ),
            api_response_callback=partial(
                _api_response_callback,
                tab=http_logs,
                response_state=st.session_state.responses,
            ),
            api_key=st.session_state.api_key,
            only_n_most_recent_images=st.session_state.only_n_most_recent_images,
        )

        # _render_download_button(json.dumps(messages))
    
        return messages


async def _reset_chat():
    with st.spinner("Resetting..."):
        st.session_state.clear()
        setup_state()
        await asyncio.sleep(1) 


def _render_save_button(messages):
    test_case_name = st.text_input("Enter Test Case Name")
    try:
        if st.button("💾 Save Test Case", type="primary"):            
            if len(test_case_name) > 0 and len(messages) > 0:
                test_case_file = test_case_name + ".json"
                logger.info(test_case_file);
                save_test_case(messages, test_case_file)
                st.success(f"Test Case saved as {test_case_file}")
    except Exception as e:
        st.error(f"Error saving test case: {e}")
        


if __name__ == "__main__":
    asyncio.run(main())
