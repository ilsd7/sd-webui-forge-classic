import json
import os
import re
import logging
from collections import defaultdict

from modules import errors

extra_network_registry = {}
extra_network_aliases = {}


def initialize():
    extra_network_registry.clear()
    extra_network_aliases.clear()


def register_extra_network(extra_network):
    extra_network_registry[extra_network.name] = extra_network


def register_extra_network_alias(extra_network, alias):
    extra_network_aliases[alias] = extra_network


class ExtraNetworkParams:
    def __init__(self, items=None):
        self.items = items or []
        self.positional = []
        self.named = {}

        for item in self.items:
            parts = item.split('=', 2) if isinstance(item, str) else [item]
            if len(parts) == 2:
                self.named[parts[0]] = parts[1]
            else:
                self.positional.append(item)

    def __eq__(self, other):
        return self.items == other.items


class ExtraNetwork:
    def __init__(self, name):
        self.name = name

    def activate(self, p, params_list):
        """
        Called by processing on every run. Whatever the extra network is meant to do should be activated here.
        Passes arguments related to this extra network in params_list.
        User passes arguments by specifying this in his prompt:

        <name:arg1:arg2:arg3>

        Where name matches the name of this ExtraNetwork object, and arg1:arg2:arg3 are any natural number of text arguments
        separated by colon.

        Even if the user does not mention this ExtraNetwork in his prompt, the call will still be made, with empty params_list -
        in this case, all effects of this extra networks should be disabled.

        Can be called multiple times before deactivate() - each new call should override the previous call completely.
        """
        raise NotImplementedError

    def deactivate(self, p):
        """
        Called at the end of processing for housekeeping. No need to do anything here.
        """

        raise NotImplementedError


def lookup_extra_networks(extra_network_data):
    """
    returns a dict mapping ExtraNetwork objects to lists of arguments for those extra networks.

    Example input:
    {
        'lora': [<modules.extra_networks.ExtraNetworkParams object at 0x0000020690D58310>],
        'lyco': [<modules.extra_networks.ExtraNetworkParams object at 0x0000020690D58F70>],
    }

    Example output:

    {
        <extra_networks_lora.ExtraNetworkLora object at 0x0000020581BEECE0>: [<modules.extra_networks.ExtraNetworkParams object at 0x0000020690D58310>, <modules.extra_networks.ExtraNetworkParams object at 0x0000020690D58F70>],
    }
    """

    res = {}

    for extra_network_name, extra_network_args in list(extra_network_data.items()):
        extra_network = extra_network_registry.get(extra_network_name, None)
        alias = extra_network_aliases.get(extra_network_name, None)

        if alias is not None and extra_network is None:
            extra_network = alias

        if extra_network is None:
            logging.info(f"Skipping unknown extra network: {extra_network_name}")
            continue

        res.setdefault(extra_network, []).extend(extra_network_args)

    return res


def activate(p, extra_network_data):
    """call activate for extra networks in extra_network_data in specified order, then call
    activate for all remaining registered networks with an empty argument list"""

    activated = []

    for extra_network, extra_network_args in lookup_extra_networks(extra_network_data).items():

        try:
            extra_network.activate(p, extra_network_args)
            activated.append(extra_network)
        except Exception as e:
            errors.display(e, f"activating extra network {extra_network.name} with arguments {extra_network_args}")

    for extra_network_name, extra_network in extra_network_registry.items():
        if extra_network in activated:
            continue

        try:
            extra_network.activate(p, [])
        except Exception as e:
            errors.display(e, f"activating extra network {extra_network_name}")

    if p.scripts is not None:
        p.scripts.after_extra_networks_activate(p, batch_number=p.iteration, prompts=p.prompts, seeds=p.seeds, subseeds=p.subseeds, extra_network_data=extra_network_data)


def deactivate(p, extra_network_data):
    """call deactivate for extra networks in extra_network_data in specified order, then call
    deactivate for all remaining registered networks"""

    data = lookup_extra_networks(extra_network_data)

    for extra_network in data:
        try:
            extra_network.deactivate(p)
        except Exception as e:
            errors.display(e, f"deactivating extra network {extra_network.name}")

    for extra_network_name, extra_network in extra_network_registry.items():
        if extra_network in data:
            continue

        try:
            extra_network.deactivate(p)
        except Exception as e:
            errors.display(e, f"deactivating unmentioned extra network {extra_network_name}")


re_extra_net = re.compile(r"<(\w+):([^>]+)>")
re_line_break = re.compile(r"\r\n?|\n")


def _is_escaped(text, index):
    backslashes = 0
    while index > 0 and text[index - 1] == "\\":
        backslashes += 1
        index -= 1

    return backslashes % 2 == 1


def _merge_extra_network_whitespace(left, right, keep_inline_space=True):
    """Collapse tag-adjacent whitespace while preserving the larger line gap."""
    left_breaks = re_line_break.findall(left)
    right_breaks = re_line_break.findall(right)
    if left_breaks or right_breaks:
        return "".join(left_breaks if len(left_breaks) >= len(right_breaks) else right_breaks)

    return " " if keep_inline_space and (left or right) else ""


def _remove_extra_network_tag(prompt, start, end):
    prompt_len = len(prompt)

    left = start
    while left > 0 and prompt[left - 1].isspace():
        left -= 1

    left_comma = left > 0 and prompt[left - 1] == "," and not _is_escaped(prompt, left - 1)
    if left_comma:
        left -= 1
        comma_index = left

        while left > 0 and prompt[left - 1].isspace():
            left -= 1

        if left > 0 and prompt[left - 1] == ",":
            # Keep whitespace that separates an unrelated preceding comma.
            left = comma_index

    right = end
    while right < prompt_len and prompt[right].isspace():
        right += 1

    right_comma = right < prompt_len and prompt[right] == ","
    if right_comma:
        right += 1

    while right < prompt_len and prompt[right].isspace():
        right += 1

    if left == 0:
        return prompt[right:]
    if right == prompt_len:
        return prompt[:left].rstrip()

    left_syntax = prompt[left - 1] in "([" and not _is_escaped(prompt, left - 1)
    right_syntax = prompt[right] in ")]"
    syntax_boundary = left_syntax or right_syntax

    left_whitespace = prompt[left:start].replace(",", "", 1)
    right_whitespace = prompt[end:right].replace(",", "", 1)
    separator = "," if not syntax_boundary and (left_comma or right_comma) else ""
    replacement = separator + _merge_extra_network_whitespace(
        left_whitespace,
        right_whitespace,
        keep_inline_space=not syntax_boundary,
    )

    return prompt[:left] + replacement + prompt[right:]


def parse_prompt(prompt):
    res = defaultdict(list)
    if "<" not in prompt:
        return prompt, res

    matches = list(re_extra_net.finditer(prompt))

    for match in matches:
        name = match.group(1)
        args = match.group(2)

        res[name].append(ExtraNetworkParams(items=args.split(":")))

    for match in reversed(matches):
        prompt = _remove_extra_network_tag(prompt, *match.span())

    return prompt, res


def parse_prompts(prompts):
    res = []
    extra_data = None

    for prompt in prompts:
        updated_prompt, parsed_extra_data = parse_prompt(prompt)

        if extra_data is None:
            extra_data = parsed_extra_data

        res.append(updated_prompt)

    return res, extra_data


def get_user_metadata(filename, lister=None):
    if filename is None:
        return {}

    basename, ext = os.path.splitext(filename)
    metadata_filename = basename + '.json'

    metadata = {}
    try:
        exists = lister.exists(metadata_filename) if lister else os.path.exists(metadata_filename)
        if exists:
            with open(metadata_filename, "r", encoding="utf8") as file:
                metadata = json.load(file)
    except Exception as e:
        errors.display(e, f"reading extra network user metadata from {metadata_filename}")

    return metadata
