from __future__ import annotations

import asyncio
import inspect
import re
from typing import TYPE_CHECKING, Any

from langchain_core.tools.structured import StructuredTool
from loguru import logger

from langflow.base.tools.constants import TOOL_OUTPUT_NAME
from langflow.io.schema import create_input_schema

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool

    from langflow.custom.custom_component.component import Component
    from langflow.inputs.inputs import InputTypes
    from langflow.io import Output


def _get_input_type(_input: InputTypes):
    if _input.input_types:
        if len(_input.input_types) == 1:
            return _input.input_types[0]
        return " | ".join(_input.input_types)
    return _input.field_type


def build_description(component: Component, output: Output) -> str:
    if not output.required_inputs:
        logger.warning(f"Output {output.name} does not have required inputs defined")

    if output.required_inputs:
        args = ", ".join(
            sorted(
                [
                    f"{input_name}: {_get_input_type(component._inputs[input_name])}"
                    for input_name in output.required_inputs
                ]
            )
        )
    else:
        args = ""
    return f"{output.method}({args}) - {component.description}"


def _build_output_function(component: Component, output_method: Callable):
    """Build a wrapper function that handles both synchronous and asynchronous output methods.

    This function creates a wrapper that:
    1. For synchronous methods: Creates a simple wrapper that sets component args and executes the method
    2. For asynchronous methods: Creates a wrapper that handles different event loop scenarios safely

    Args:
        component (Component): The component instance that contains the method
        output_method (Callable): The method to be wrapped, can be either sync or async

    Returns:
        Callable: A wrapped function that handles the appropriate execution context.
    """
    # Handle synchronous methods with a simple wrapper
    if not is_async_callable(output_method):

        def sync_function(*args, **kwargs):
            """Synchronous wrapper that sets component arguments and executes the method.

            Args:
                *args: Positional arguments to be passed to component.set()
                **kwargs: Keyword arguments to be passed to component.set()

            Returns:
                Any: The result of the output_method execution
            """
            component.set(*args, **kwargs)
            return output_method()

        return sync_function

    # Handle asynchronous methods with a wrapper that manages event loops
    def async_wrapper(*args, **kwargs):
        """Handle asynchronous execution of component methods while managing event loops.

        This wrapper provides a synchronous interface to asynchronous methods by:
        1. Setting component arguments using the provided args/kwargs
        2. Managing the asyncio event loop lifecycle
        3. Handling cleanup of pending tasks to prevent resource leaks

        Args:
            *args: Positional arguments to be passed to component.set()
            **kwargs: Keyword arguments to be passed to component.set()

        Returns:
            Any: The result of the asynchronous operation

        Raises:
            RuntimeError: If there are issues with the event loop management
        """
        component.set(*args, **kwargs)

        async def _run_async():
            return await output_method()

        try:
            loop = asyncio.get_running_loop()
            return loop.run_until_complete(_run_async())
        except RuntimeError:
            # No running loop
            return asyncio.run(_run_async())
        finally:
            # Ensure we don't leave any pending tasks
            pending = asyncio.all_tasks(loop) if "loop" in locals() else set()
            for task in pending:
                task.cancel()

    return async_wrapper


def _format_tool_name(name: str):
    # format to '^[a-zA-Z0-9_-]+$'."
    # to do that we must remove all non-alphanumeric characters

    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


def is_async_callable(obj: Any) -> bool:
    if not callable(obj):
        return False
    if asyncio.iscoroutinefunction(obj):
        return True
    if inspect.isclass(obj):
        return False
    if callable(obj):
        return asyncio.iscoroutinefunction(obj.__call__)
    return False


class ComponentToolkit:
    def __init__(self, component: Component):
        self.component = component

    def get_tools(self) -> list[BaseTool]:
        tools = []
        for output in self.component.outputs:
            if output.name == TOOL_OUTPUT_NAME:
                continue

            if not output.method:
                msg = f"Output {output.name} does not have a method defined"
                raise ValueError(msg)

            output_method: Callable = getattr(self.component, output.method)
            # TODO: check if the coutput method is async and make it synchronousd

            args_schema = None
            if output.required_inputs:
                inputs = [self.component._inputs[input_name] for input_name in output.required_inputs]
                args_schema = create_input_schema(inputs)
            else:
                args_schema = create_input_schema(self.component.inputs)
            name = f"{self.component.name}.{output.method}"
            formatted_name = _format_tool_name(name)
            tools.append(
                StructuredTool(
                    name=formatted_name,
                    description=build_description(self.component, output),
                    func=_build_output_function(self.component, output_method),
                    args_schema=args_schema,
                )
            )
        return tools
