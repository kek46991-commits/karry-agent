"""karry-agent の対話型 CLI。"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from dotenv import load_dotenv

from .core import AgentCore
from .logger import log_progress


def run_interactive(agent: AgentCore | None = None, input_fn: Callable[[str], str] = input) -> int:
    agent = agent or AgentCore()
    print("=== karry-agent CLI Mode ===")
    print("Type 'exit' or 'quit' to end the session.\n")
    log_progress("CLI Started", "User initiated CLI session.")

    while True:
        try:
            user_input = input_fn("User > ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                print("Exiting karry-agent CLI. Goodbye!")
                log_progress("CLI Ended", "User terminated session.")
                return 0

            print("\n[karry-agent is thinking...]")
            result = agent.run(user_input)
            response = result.text
            print(f"\nAgent > {response}\n")
            log_progress("Task Processed", f"Input: {user_input} | Result: {response[:200]}")
        except KeyboardInterrupt:
            print("\nSession interrupted. Exiting...")
            log_progress("CLI Interrupted", "KeyboardInterrupt received.")
            return 130
        except EOFError:
            print("\nEnd of input. Exiting...")
            log_progress("CLI Ended", "Input stream closed.")
            return 0
        except Exception as exc:
            print(f"\nError: {exc}\n")
            log_progress("Error", str(exc))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="karry-agent Gemini function-calling CLI")
    parser.add_argument("prompt", nargs="?", help="単発で実行するプロンプト")
    args = parser.parse_args(argv)
    if args.prompt:
        result = AgentCore().run(args.prompt)
        print(result.text)
        log_progress("Task Processed", f"Input: {args.prompt} | Result: {result.text[:200]}")
        return 0
    return run_interactive()


if __name__ == "__main__":
    sys.exit(main())
