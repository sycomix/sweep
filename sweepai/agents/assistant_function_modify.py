import traceback

from loguru import logger

from sweepai.agents.assistant_functions import search_and_replace_schema
from sweepai.agents.assistant_wrapper import openai_assistant_call
from sweepai.core.entities import AssistantRaisedException, Message
from sweepai.logn.cache import file_cache
from sweepai.utils.chat_logger import ChatLogger, discord_log_error
from sweepai.utils.diff import generate_diff
from sweepai.utils.progress import AssistantConversation, TicketProgress
from sweepai.utils.utils import check_code, chunk_code

instructions = """You are a brilliant and meticulous engineer assigned to write code to complete the user's request. When you write code, the code works on the first try, and is complete. Take into account the current repository's language, code style, and dependencies. Your job is to make edits to the file to complete the user "# Request".

# Instructions
Modify the snippets above according to the request by calling the search_and_replace function.
* Keep whitespace and comments.
* Make the minimum necessary search_and_replaces to make changes to the snippets. Only write diffs for lines that should be changed.
* Write multiple small changes instead of a single large change."""


def int_to_excel_col(n):
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def excel_col_to_int(s):
    result = 0
    for char in s:
        result = result * 26 + (ord(char) - 64)
    return result - 1


MAX_CHARS = 32000


@file_cache(ignore_params=["file_path", "chat_logger"])
def function_modify(
    request: str,
    file_path: str,
    file_contents: str,
    additional_messages: list[Message] = [],
    chat_logger: ChatLogger | None = None,
    assistant_id: str = None,
    start_line: int = -1,
    end_line: int = -1,
    ticket_progress: TicketProgress | None = None,
    assistant_conversation: AssistantConversation | None = None,
    seed: int = None,
):
    try:

        def save_ticket_progress(assistant_id: str, thread_id: str, run_id: str):
            if assistant_conversation:
                assistant_conversation.update_from_ids(
                    assistant_id=assistant_id, run_id=run_id, thread_id=thread_id
                )
            ticket_progress.save()

        current_contents = file_contents

        original_snippets = chunk_code(current_contents, file_path, 700, 200)
        file_contents_lines = current_contents.split("\n")
        chunks = [
            "\n".join(file_contents_lines[snippet.start : snippet.end])
            for snippet in original_snippets
        ]
        code_sections = []
        current_code_section = ""
        for i, chunk in enumerate(chunks):
            idx = int_to_excel_col(i + 1)
            section_display = f'<section id="{idx}">\n{chunk}\n</section id="{idx}">'
            if len(current_code_section) + len(section_display) > MAX_CHARS:
                code_sections_string = f"# Code\nFile path:{file_path}\n<sections>\n{current_code_section}\n</sections>"
                code_sections.append(code_sections_string)
                current_code_section = section_display
            else:
                current_code_section += section_display
        code_sections.append(current_code_section)
        code_sections_string = "\n".join(code_sections)
        additional_messages += [
            *reversed(
                [
                    Message(
                        role="user",
                        content=code_section,
                    )
                    for code_section in code_sections
                ]
            ),
            Message(
                role="user",
                content=f"# Request\n{request}",
            ),
        ]
        assistant_generator = openai_assistant_call(
            request=request,
            instructions=instructions,
            additional_messages=additional_messages,
            chat_logger=chat_logger,
            assistant_id=assistant_id,
            save_ticket_progress=save_ticket_progress
            if ticket_progress is not None
            else None,
            assistant_name="Code Modification Function Assistant",
            tools=[
                {"type": "code_interpreter"},
                {"type": "function", "function": search_and_replace_schema},
            ],
        )
        try:
            tool_name, tool_call = assistant_generator.send(None)
            for i in range(50):
                print(tool_name, tool_call)
                if tool_name == "search_and_replace":
                    error_message = ""
                    success_message = ""
                    prev_contents = current_contents

                    for replace_to_make in tool_call["replaces_to_make"]:
                        if "section_id" not in replace_to_make:
                            error_message = "Missing section_id in replace_to_make"
                            break
                        section_letter = replace_to_make["section_id"]
                        section_id = excel_col_to_int(section_letter)
                        old_code = replace_to_make["old_code"]
                        new_code = replace_to_make["new_code"]

                        if section_id >= len(chunks):
                            error_message = f"Could not find section {section_letter} in file {file_path}, which has {len(chunks)} sections."
                            break
                        chunk = chunks[section_id]
                        if old_code not in chunk:
                            error_message = f"Could not find the old_code:\n```\n{old_code}\n```\nIn section {section_id}, which has code:\n```\n{chunk}\n```"
                            break
                        new_code_section = chunk.replace(old_code, new_code)
                        new_contents = current_contents.replace(chunk, new_code_section)

                    if not error_message:
                        is_valid, message = check_code(file_path, new_contents)
                        if is_valid:
                            success_message = generate_diff(
                                current_contents, new_contents
                            )
                            current_contents = new_contents

                            # Re-initialize
                            original_snippets = chunk_code(
                                current_contents, file_path, 700, 200
                            )
                            file_contents_lines = current_contents.split("\n")
                            chunks = [
                                "\n".join(
                                    file_contents_lines[snippet.start : snippet.end]
                                )
                                for snippet in original_snippets
                            ]
                            code_sections = []
                            current_code_section = ""
                            for i, chunk in enumerate(chunks):
                                idx = int_to_excel_col(i + 1)
                                section_display = f'<section id="{idx}">\n{chunk}\n</section id="{idx}">'
                                if (
                                    len(current_code_section) + len(section_display)
                                    > MAX_CHARS
                                ):
                                    code_sections_string = f"# Code\nFile path:{file_path}\n<sections>\n{current_code_section}\n</sections>"
                                    code_sections.append(code_sections_string)
                                    current_code_section = section_display
                                else:
                                    current_code_section += section_display
                            code_sections.append(current_code_section)
                            success_message += f"\n\n{code_sections[0]}"
                        else:
                            diff = generate_diff(current_contents, new_contents)
                            error_message = f"When the following changes are applied:\n```diff\n{diff}\n```\nIt yields invalid code with the following message:\n```{message}```\nRemake the changes with additional changes ."
                            current_contents = prev_contents

                    if error_message:
                        logger.error(error_message)
                        tool_name, tool_call = assistant_generator.send(
                            f"ERROR\nNo changes we're made due to the following error:\n\n{error_message}"
                        )
                    else:
                        logger.info(error_message)
                        tool_name, tool_call = assistant_generator.send(
                            f"SUCCESS\nHere are the new code sections:\n\n{success_message}"
                        )
                else:
                    raise Exception("Unexpected tool name")
        except StopIteration:
            pass
        diff = generate_diff(file_contents, current_contents)
        if diff:
            logger.info("Changes made:")
            logger.info(diff[: min(1000, len(diff))])
        else:
            logger.warning("No changes were made.")
        if ticket_progress:
            save_ticket_progress(
                assistant_id=response.assistant_id,
                thread_id=response.thread_id,
                run_id=response.run_id,
            )
        if current_contents != file_contents:
            return current_contents
    except AssistantRaisedException as e:
        logger.exception(e)
        discord_log_error(
            str(e)
            + "\n\n"
            + traceback.format_exc()
            + "\n\n"
            + str(chat_logger.data if chat_logger else "")
        )
    except Exception as e:
        logger.exception(e)
        # TODO: Discord
        discord_log_error(
            str(e)
            + "\n\n"
            + traceback.format_exc()
            + "\n\n"
            + str(chat_logger.data if chat_logger else "")
        )
        return None
    return None


if __name__ == "__main__":
    request = """• Replace the broken installation link with the provided new link.\n• Change the text from "check out our [tutorial on running Sweep on Docusaurus](https://docs.sweep.dev/usage/tutorial)." \n  to "check out our [tutorial on running Sweep on Docusaurus](https://docs.sweep.dev/tutorial).\""""
    request = "convert any unnecessary logger.exceptions to logger.errors in api.py"
    # additional_messages = [
    #     Message(
    #         role="user",
    #         content="# Repo & Issue Metadata\nRepo: sweep: Sweep: AI-powered Junior Developer for small features and bug fixes.\nIssue Title: replace the broken installation link in installation.md with https://docs.sweep.dev/tutorial",
    #         name=None,
    #         function_call=None,
    #         key="issue_metadata",
    #     )
    # ]
    file_contents = open("sweepai/handlers/on_ticket.py", "r").read()
    response = function_modify(
        request=request,
        file_path="sweepai/handlers/on_ticket.py",
        file_contents=file_contents,
        chat_logger=ChatLogger({"username": "wwzeng1"}),
        # additional_messages=additional_messages,
    )
