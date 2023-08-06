from __future__ import annotations

import collections
import datetime  # noqa
import functools
import inspect
import json
import os
import re  # noqa
import shutil
import typing  # noqa
from pathlib import Path
from typing import *  # noqa

import click
import inflect
from black import FileMode, WriteBack, format_file_in_place
from jinja2 import Environment
from packaging import version

import coredis
import coredis.client
import coredis.pipeline
from coredis.commands.constants import *  # noqa
from coredis.commands.monitor import Monitor
from coredis.pool import ClusterConnectionPool, ConnectionPool  # noqa
from coredis.response.types import *  # noqa
from coredis.tokens import PureToken  # noqa
from coredis.typing import *  # noqa

MAX_SUPPORTED_VERSION = version.parse("7.999.999")
MIN_SUPPORTED_VERSION = version.parse("5.999.999")

MODULES = {
    "RedisJSON": {
        "repo": "https://github.com/RedisJSON/RedisJSON",
        "prefix": "json",
        "group": "json",
        "module": "RedisJSON",
    },
    "bloom": {
        "repo": "https://github.com/RedisBloom/RedisBloom",
        "prefix": "bf",
        "group": "bf",
        "module": "bf",
    },
    "cuckoo": {
        "repo": "https://github.com/RedisBloom/RedisBloom",
        "prefix": "cf",
        "group": "cf",
        "module": "bf",
    },
    "countmin": {
        "repo": "https://github.com/RedisBloom/RedisBloom",
        "prefix": "cms",
        "group": "cms",
        "module": "bf",
    },
    "topk": {
        "repo": "https://github.com/RedisBloom/RedisBloom",
        "prefix": "topk",
        "group": "topk",
        "module": "bf",
    },
    "tdigest": {
        "repo": "https://github.com/RedisBloom/RedisBloom",
        "prefix": "tdigest",
        "group": "tdigest",
        "module": "bf",
    },
    "timeseries": {
        "repo": "https://github.com/RedisTimeSeries/RedisTimeSeries/",
        "prefix": "ts",
        "group": "timeseries",
        "module": "timeseries",
    },
    "graph": {
        "repo": "https://github.com/RedisGraph/RedisGraph/",
        "prefix": "graph",
        "group": "graph",
        "module": "graph",
    },
    "search": {
        "repo": "https://github.com/RediSearch/RediSearch/",
        "prefix": "ft",
        "group": "search",
        "module": "search",
    },
    "suggestion": {
        "repo": "https://github.com/RediSearch/RediSearch/",
        "prefix": "ft",
        "group": "suggestion",
        "module": "search",
    },
}

MAPPING = {"DEL": "delete"}
SKIP_SPEC = ["BITFIELD", "BITFIELD_RO"]
SKIP_COMMANDS = [
    "REPLCONF",
    "PFDEBUG",
    "PFSELFTEST",
    "PSYNC",
    "RESTORE-ASKING",
    "SYNC",
    "XSETID",
]

REDIS_ARGUMENT_TYPE_MAPPING = {
    "array": Sequence,
    "simple-string": StringT,
    "bulk-string": StringT,
    "string": StringT,
    "pattern": StringT,
    "key": KeyT,
    "integer": int,
    "double": Union[int, float],
    "unix-time": Union[int, datetime.datetime],
    "pure-token": bool,
}
REDIS_RETURN_ARGUMENT_TYPE_MAPPING = {
    **REDIS_ARGUMENT_TYPE_MAPPING,
    **{
        "simple-string": AnyStr,
        "bulk-string": AnyStr,
        "string": AnyStr,
        "array": Tuple,
        "double": Union[int, float],
        "unix-time": "datetime.datetime",
        "pure-token": bool,
    },
}
REDIS_ARGUMENT_NAME_OVERRIDES = {
    "BITPOS": {"end_index_index_unit": "index_unit"},
    "BITCOUNT": {"index_index_unit": "index_unit"},
    "CLUSTER ADDSLOTSRANGE": {"start_slot_end_slot": "slots"},
    "CLUSTER DELSLOTSRANGE": {"start_slot_end_slot": "slots"},
    "CLIENT REPLY": {"on_off_skip": "mode"},
    "GEOSEARCH": {"bybox": "width", "byradius": "radius", "frommember": "member"},
    "GEOSEARCHSTORE": {"bybox": "width", "byradius": "radius", "frommember": "member"},
    "SORT": {"sorting": "alpha"},
    "SORT_RO": {"sorting": "alpha"},
    "SCRIPT FLUSH": {"async": "sync_type"},
    "ZADD": {"score_member": "member_score"},
    "ZRANGE": {"start": "min", "stop": "max"},
}
REDIS_ARGUMENT_TYPE_OVERRIDES = {
    "CLIENT KILL": {"skipme": bool},
    "COMMAND GETKEYS": {"arguments": ValueT},
    "COMMAND GETKEYSANDFLAGS": {"arguments": ValueT},
    "FUNCTION RESTORE": {"serialized_value": bytes},
    "MSET": {"key_values": Dict[KeyT, ValueT]},
    "MSETNX": {"key_values": Dict[KeyT, ValueT]},
    "HMSET": {"field_values": Dict[StringT, ValueT]},
    "HSET": {"field_values": Dict[StringT, ValueT]},
    "MIGRATE": {"port": int},
    "RESTORE": {
        "serialized_value": bytes,
        "ttl": Union[int, datetime.timedelta, datetime.datetime],
    },
    "SORT": {"gets": KeyT},
    "SORT_RO": {"gets": KeyT},
    "XADD": {"field_values": Dict[StringT, ValueT], "threshold": Optional[int]},
    "XAUTOCLAIM": {"min_idle_time": Union[int, datetime.timedelta]},
    "XCLAIM": {
        "min_idle_time": Union[int, datetime.timedelta],
        "ms": Optional[Union[int, datetime.timedelta]],
    },
    "XREAD": {"streams": Mapping[ValueT, ValueT]},
    "XREADGROUP": {"streams": Mapping[ValueT, ValueT]},
    "XTRIM": {"threshold": int},
    "ZADD": {"member_scores": Dict[StringT, float]},
    "ZCOUNT": {"min": Union[ValueT, float], "max": Union[ValueT, float]},
    "ZREVRANGE": {"min": Union[int, ValueT], "max": Union[int, ValueT]},
    "ZRANGE": {"start": Union[int, ValueT], "stop": Union[int, ValueT]},
    "ZRANGESTORE": {"min": Union[int, ValueT], "max": Union[int, ValueT]},
}
IGNORED_ARGUMENTS = {
    "FCALL": ["numkeys"],
    "FCALL_RO": ["numkeys"],
    "LMPOP": ["numkeys"],
    "BLMPOP": ["numkeys"],
    "BZMPOP": ["numkeys"],
    "EVAL": ["numkeys"],
    "EVAL_RO": ["numkeys"],
    "EVALSHA": ["numkeys"],
    "EVALSHA_RO": ["numkeys"],
    "MIGRATE": ["key-selector"],
    "SINTERCARD": ["numkeys"],
    "ZDIFF": ["numkeys"],
    "ZDIFFSTORE": ["numkeys"],
    "ZINTER": ["numkeys"],
    "ZINTERCARD": ["numkeys"],
    "ZINTERSTORE": ["numkeys"],
    "ZMPOP": ["numkeys"],
    "ZUNION": ["numkeys"],
    "ZUNIONSTORE": ["numkeys"],
    "XADD": ["id-selector"],
    "XGROUP CREATE": ["new_id"],
    "XGROUP SETID": ["new_id"],
    "TS.MGET": ["filterExpr"],
    "TS.RANGE": ["filterExpr"],
    "TS.REVRANGE": ["filterExpr"],
    "TS.MRANGE": ["filterExpr"],
    "TS.MREVRANGE": ["filterExpr"],
}
REDIS_RETURN_OVERRIDES = {
    "ACL USERS": Tuple[AnyStr, ...],
    "ACL GETUSER": Dict[AnyStr, List[AnyStr]],
    "ACL LIST": Tuple[AnyStr, ...],
    "ACL LOG": Union[bool, Tuple[Dict[AnyStr, AnyStr], ...]],
    "BZPOPMAX": Optional[Tuple[AnyStr, AnyStr, float]],
    "BZPOPMIN": Optional[Tuple[AnyStr, AnyStr, float]],
    "BZMPOP": Optional[Tuple[AnyStr, ScoredMembers]],
    "CLIENT LIST": Tuple[ClientInfo, ...],
    "CLIENT INFO": ClientInfo,
    "CLUSTER BUMPEPOCH": AnyStr,
    "CLUSTER INFO": Dict[str, str],
    "CLUSTER LINKS": List[Dict[AnyStr, ResponseType]],
    "CLUSTER NODES": List[ClusterNodeDetail],
    "CLUSTER REPLICAS": List[ClusterNodeDetail],
    "CLUSTER SHARDS": List[Dict[AnyStr, Union[List[ValueT], Mapping[AnyStr, ValueT]]]],
    "CLUSTER SLAVES": List[ClusterNodeDetail],
    "CLUSTER SLOTS": Dict[Tuple[int, int], Tuple[ClusterNode, ...]],
    "COMMAND": Dict[str, Command],
    "COMMAND DOCS": Dict[AnyStr, Dict],
    "COMMAND GETKEYSANDFLAGS": Dict[AnyStr, Set[AnyStr]],
    "COMMAND INFO": Dict[str, Command],
    "COMMAND LIST": Set[AnyStr],
    "CONFIG GET": Dict[AnyStr, AnyStr],
    "DUMP": bytes,
    "EXPIRETIME": "datetime.datetime",
    "EVAL": ResponseType,
    "EVAL_RO": ResponseType,
    "EVALSHA": ResponseType,
    "EVALSHA_RO": ResponseType,
    "FCALL": ResponseType,
    "FCALL_RO": ResponseType,
    "FUNCTION DUMP": bytes,
    "FUNCTION LOAD": AnyStr,
    "FUNCTION STATS": Dict[
        AnyStr, Union[AnyStr, Dict[AnyStr, Dict[AnyStr, ResponsePrimitive]]]
    ],
    "FUNCTION LIST": Dict[str, LibraryDefinition],
    "GEODIST": Optional[float],
    "GEOPOS": Tuple[Optional[GeoCoordinates], ...],
    "GEOSEARCH": Union[int, Tuple[Union[AnyStr, GeoSearchResult], ...]],
    "GEORADIUSBYMEMBER": Union[int, Tuple[Union[AnyStr, GeoSearchResult], ...]],
    "GEORADIUS": Union[int, Tuple[Union[AnyStr, GeoSearchResult], ...]],
    "HELLO": Dict[AnyStr, AnyStr],
    "HINCRBYFLOAT": float,
    "HRANDFIELD": Union[AnyStr, Tuple[AnyStr, ...], Dict[AnyStr, AnyStr]],
    "HMGET": Tuple[Optional[AnyStr], ...],
    "HSCAN": Tuple[int, Dict[AnyStr, AnyStr]],
    "INCRBYFLOAT": float,
    "INFO": Dict[str, ResponseType],
    "KEYS": "Set[AnyStr]",
    "LASTSAVE": "datetime.datetime",
    "LATENCY LATEST": Dict[AnyStr, Tuple[int, int, int]],
    "LATENCY HISTOGRAM": Dict[AnyStr, Dict[AnyStr, Any]],
    "LCS": Union[AnyStr, int, LCSResult],
    "LPOS": Optional[Union[int, List[int]]],
    "MEMORY STATS": Dict[AnyStr, Union[AnyStr, int, float]],
    "MGET": Tuple[Optional[AnyStr], ...],
    "MODULE LIST": Tuple[Dict, ...],
    "MONITOR": Monitor,
    "PING": AnyStr,
    "PFADD": bool,
    "PSETEX": bool,
    "PEXPIRETIME": "datetime.datetime",
    "PUBSUB NUMSUB": OrderedDict[AnyStr, int],
    "RPOPLPUSH": Optional[AnyStr],
    "RESET": None,
    "ROLE": RoleInfo,
    "SCAN": Tuple[int, Tuple[AnyStr, ...]],
    "SMISMEMBER": Tuple[bool, ...],
    "SCRIPT EXISTS": Tuple[bool, ...],
    "SLOWLOG GET": Tuple[SlowLogInfo, ...],
    "SSCAN": Tuple[int, Set[AnyStr]],
    "TIME": "datetime.datetime",
    "TYPE": Optional[AnyStr],
    "XCLAIM": Union[Tuple[AnyStr, ...], Tuple[StreamEntry, ...]],
    "XAUTOCLAIM": Union[
        Tuple[AnyStr, Tuple[AnyStr, ...]],
        Tuple[AnyStr, Tuple[StreamEntry, ...], Tuple[AnyStr, ...]],
    ],
    "XGROUP CREATECONSUMER": bool,
    "XINFO GROUPS": Tuple[Dict[AnyStr, AnyStr], ...],
    "XINFO CONSUMERS": Tuple[Dict[AnyStr, AnyStr], ...],
    "XINFO STREAM": StreamInfo,
    "XPENDING": Union[Tuple[StreamPendingExt, ...], StreamPending],
    "XRANGE": Tuple[StreamEntry, ...],
    "XREVRANGE": Tuple[StreamEntry, ...],
    "XREADGROUP": Optional[Dict[AnyStr, Tuple[StreamEntry, ...]]],
    "XREAD": Optional[Dict[AnyStr, Tuple[StreamEntry, ...]]],
    "ZDIFF": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZINTER": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZMPOP": Optional[Tuple[AnyStr, ScoredMembers]],
    "ZPOPMAX": Union[ScoredMember, ScoredMembers],
    "ZPOPMIN": Union[ScoredMember, ScoredMembers],
    "ZRANDMEMBER": Optional[Union[AnyStr, List[AnyStr], ScoredMembers]],
    "ZRANGE": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZRANGEBYSCORE": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZREVRANGEBYSCORE": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZREVRANGE": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZRANK": Optional[Union[int, Tuple[int, float]]],
    "ZREVRANK": Optional[Union[int, Tuple[int, float]]],
    "ZUNION": Tuple[Union[AnyStr, ScoredMember], ...],
    "ZSCAN": Tuple[int, ScoredMembers],
    "ZSCORE": Optional[float],
}
ARGUMENT_DEFAULTS = {
    "HSCAN": {"cursor": 0},
    "SCAN": {"cursor": 0},
    "SSCAN": {"cursor": 0},
    "ZSCAN": {"cursor": 0},
}
ARGUMENT_DEFAULTS_NON_OPTIONAL = {
    "KEYS": {"pattern": "*"},
}
ARGUMENT_OPTIONALITY = {
    "EVAL_RO": {"key": True, "arg": True},
    "EVALSHA_RO": {"key": True, "arg": True},
    "FCALL": {"key": True, "arg": True},
    "FCALL_RO": {"key": True, "arg": True},
    "HSCAN": {"cursor": True},
    "MIGRATE": {"keys": False},
    "SLAVEOF": {"host": True, "port": True},
    "REPLICAOF": {"host": True, "port": True},
    "SCAN": {"cursor": True},
    "SSCAN": {"cursor": True},
    "XADD": {"id_or_auto": True},
    "XRANGE": {"start": True, "end": True},
    "XREVRANGE": {"start": True, "end": True},
    "ZSCAN": {"cursor": True},
}
ARGUMENT_VARIADICITY = {"SORT": {"gets": False}, "SORT_RO": {"gets": False}}
REDIS_ARGUMENT_FORCED_ORDER = {
    "SETEX": ["key", "value", "seconds"],
    "ZINCRBY": ["key", "member", "increment"],
    "EVALSHA": ["sha1", "keys", "args"],
    "EVALSHA_RO": ["sha1", "keys", "args"],
    "EVAL": ["script", "keys", "args"],
    "EVAL_RO": ["script", "keys", "args"],
    "CLIENT KILL": [
        "ip_port",
        "identifier",
        "type_",
        "user",
        "addr",
        "laddr",
        "skipme",
    ],
    "CLIENT LIST": ["type", "identifiers"],
    "FCALL": ["function", "keys", "args"],
    "FCALL_RO": ["function", "keys", "args"],
}
REDIS_ARGUMENT_FORCED = {
    "COMMAND GETKEYS": [
        {"name": "arguments", "type": "bulk-string", "multiple": True},
    ],
    "COMMAND GETKEYSANDFLAGS": [
        {"name": "arguments", "type": "bulk-string", "multiple": True},
    ],
}
READONLY_OVERRIDES = {"TOUCH": False}
BLOCK_ARGUMENT_FORCED_ORDER = {"ZADD": {"member_scores": ["member", "score"]}}
STD_GROUPS = [
    "generic",
    "string",
    "bitmap",
    "hash",
    "list",
    "set",
    "sorted-set",
    "hyperloglog",
    "geo",
    "stream",
    "scripting",
    "pubsub",
    "transactions",
]

VERSIONADDED_DOC = re.compile(r"(.. versionadded:: ([\d\.]+))", re.DOTALL)
VERSIONCHANGED_DOC = re.compile(r"(.. versionchanged:: ([\d\.]+))", re.DOTALL)

ROUTE_MAPPING = {
    "all_nodes": NodeFlag.ALL,
    "all_shards": NodeFlag.PRIMARIES,
}

MERGE_MAPPING = {
    "one_succeeded": ["first response", "the first response that is not an error"],
    "all_succeeded": [
        "success if all shards responded ``True``",
        "the response from any shard if all responses are consistent",
    ],
    "agg_logical_and": ["the logical AND of all responses"],
    "agg_logical_or": ["the logical OR of all responses"],
    "agg_sum": ["the sum of results"],
}

inflection_engine = inflect.engine()


def command_enum(command_name) -> str:
    return "CommandName." + command_name.upper().replace(" ", "_").replace("-", "_")


def sanitized_rendered_type(rendered_type) -> str:
    v = re.sub("<class '(.*?)'>", "\\1", rendered_type)
    v = re.sub("<PureToken.(.*?): b'(.*?)'>", "PureToken.\\1", v)
    v = v.replace("~str", "str")
    v = v.replace("~AnyStr", "AnyStr")
    v = re.sub(r"typing\.(.*?)", "\\1", v)
    v = v.replace("Ellipsis", "...")
    v = v.replace("coredis.response.types.", "")
    v = v.replace("coredis.pool.", "")
    return v


def render_annotation(annotation):
    if not annotation:
        return "None"

    if isinstance(annotation, type) and not hasattr(annotation, "__args__"):
        if not annotation.__name__ == "NoneType":
            return annotation.__name__

        if not annotation.__name__ == "Ellipsis":
            return None
    else:
        if hasattr(annotation, "__name__"):
            if hasattr(annotation, "__args__"):
                if annotation.__name__ == "Union":
                    args = list(annotation.__args__)
                    none_seen = False
                    for a in annotation.__args__:
                        if getattr(a, "__name__", "NotNoneType") == "NoneType":
                            args.remove(a)
                            none_seen = True
                        if getattr(a, "__name__", "NotEllipsis") == "Ellipsis":
                            args.remove(a)

                    if none_seen:
                        if len(args) > 1:
                            return sanitized_rendered_type(
                                f"Optional[{Union[tuple(args)]}]"
                            )
                        else:
                            return sanitized_rendered_type(f"Optiona[{args[0]}]")

                else:
                    sub_annotations = [
                        render_annotation(arg) for arg in annotation.__args__
                    ]
                    sub = ", ".join([k for k in sub_annotations if k])

                    return sanitized_rendered_type(f"{annotation.__name__}[{sub}]")

        return sanitized_rendered_type(str(annotation))


def version_changed_from_doc(doc):
    if not doc:
        return
    v = VERSIONCHANGED_DOC.findall(doc)

    if v:
        return version.parse(v[0][1])


def version_added_from_doc(doc):
    if not doc:
        return
    v = VERSIONADDED_DOC.findall(doc)

    if v:
        return version.parse(v[0][1])


@functools.lru_cache
def get_commands(name: str = "commands.json"):
    cur_dir = os.path.split(__file__)[0]
    return json.loads(open(os.path.join(cur_dir, name)).read())


def sanitize_parameter(p, eval_forward_annotations=True):
    if isinstance(p.annotation, str) and eval_forward_annotations:
        p = p.replace(annotation=eval(p.annotation))

    v = sanitized_rendered_type(str(p))
    if (
        hasattr(p, "annotation")
        and p.annotation != inspect._empty
        and hasattr(p.annotation, "__args__")
    ):
        annotation_args = p.annotation.__args__
        if any(
            getattr(a, "__name__", "NotNoneType") == "NoneType" for a in annotation_args
        ):
            new_args = [
                a
                for a in annotation_args
                if getattr(a, "__name__", "NotNoneType") != "NoneType"
            ]
            if len(new_args) == 1:
                v = re.sub(r"Union\[([\w,\s\[\]\.]+), NoneType\]", "Optional[\\1]", v)
            else:
                v = re.sub(
                    r"Union\[([\w,\s\[\]\.]+), NoneType\]", "Optional[Union[\\1]]", v
                )
    return v


def render_signature(
    signature, eval_forward_annotations=False, skip_defaults=False, with_return=True
):
    params = []
    for n, p in signature.parameters.items():
        if skip_defaults and p.default is not inspect._empty:
            p = p.replace(default=Ellipsis)
        params.append(sanitize_parameter(p, eval_forward_annotations))
    if with_return and signature.return_annotation != inspect._empty:
        return (
            f"({', '.join(params)}) -> {render_annotation(signature.return_annotation)}"
        )
    else:
        return f"({', '.join(params)})"


def compare_signatures(s1, s2, eval_forward_annotations=True, with_return=True):
    return render_signature(
        s1, eval_forward_annotations, with_return=with_return
    ) == render_signature(s2, eval_forward_annotations, with_return=with_return)


def get_token_mapping():
    pure_token_mapping = collections.OrderedDict(
        {
            ("unique", "UNIQUE"): {"GRAPH.CONTRAINT DROP", "GRAPH.CONSTRAINT CREATE"},
            ("mandatory", "MANDATORY"): {
                "GRAPH.CONTRAINT DROP",
                "GRAPH.CONSTRAINT CREATE",
            },
        }
    )
    prefix_token_mapping = collections.OrderedDict(
        {
            ("node", "NODE"): {"GRAPH.CONTRAINT DROP", "GRAPH.CONSTRAINT CREATE"},
            ("relationship", "RELATIONSHIP"): {
                "GRAPH.CONTRAINT DROP",
                "GRAPH.CONSTRAINT CREATE",
            },
            ("properties", "PROPERTIES"): {
                "GRAPH.CONTRAINT DROP",
                "GRAPH.CONSTRAINT CREATE",
            },
        }
    )

    for name in ["commands", *MODULES.keys()]:
        commands = get_commands(name + ".json")
        for command, details in commands.items():
            if name in MODULES and not details["group"] == MODULES[name]["group"]:
                continue

            def _extract_pure_tokens(obj):
                tokens = []

                if args := obj.get("arguments"):
                    for arg in args:
                        if arg["type"] == "pure-token":
                            tokens.append(
                                (
                                    sanitized(arg["name"], ignore_reserved_words=True),
                                    arg["token"].upper(),
                                )
                            )

                        if arg.get("arguments"):
                            tokens.extend(_extract_pure_tokens(arg))

                return tokens

            def _extract_prefix_tokens(obj):
                tokens = []

                if args := obj.get("arguments"):
                    for arg in args:
                        if arg["type"] != "pure-token" and arg.get("token"):
                            tokens.append(
                                (
                                    sanitized(arg["token"], ignore_reserved_words=True),
                                    arg["token"].upper(),
                                )
                            )

                        if arg.get("arguments"):
                            tokens.extend(_extract_prefix_tokens(arg))

                return tokens

            for token in sorted(
                _extract_pure_tokens(details), key=lambda token: token[0]
            ):
                pure_token_mapping.setdefault(token, set()).add(command)
            for token in sorted(
                _extract_prefix_tokens(details), key=lambda token: token[0]
            ):
                prefix_token_mapping.setdefault(token, set()).add(command)
    return pure_token_mapping, prefix_token_mapping


def read_command_docs(command, group, module=None):
    if not module:
        doc = open(
            "/var/tmp/redis-doc/commands/%s.md" % command.lower().replace(" ", "-")
        ).read()
    else:
        doc = open(
            f"/var/tmp/redis-module-{module}/docs/commands/%s.md"
            % command.lower().replace(" ", "-")
        ).read()

    return_description = re.compile(
        r"(@(.*?)-reply[:,]*\s*(.*?)$)", re.MULTILINE
    ).findall(doc)

    def sanitize_description(desc):
        if not desc:
            return ""
        return_description = (
            desc.replace("a nil bulk reply", "``None``")
            .replace("a null bulk reply", "``None``")
            .replace(", specifically:", "")
            .replace("specifically:", "")
            .replace("represented as a string", "")
        )
        return_description = re.sub("`(.*?)`", "``\\1``", return_description)
        return_description = return_description.replace("`nil`", "``None``")
        return_description = re.sub("_(.*?)_", "``\\1``", return_description)
        return_description = return_description.replace(
            "````None````", "``None``"
        )  # lol
        return_description = return_description.replace("@examples", "")  # more lol
        return_description = return_description.replace("@example", "")  # more more lol
        return_description = re.sub(r"^\s*([^\w]+)", "", return_description)

        return return_description

    full_description = re.compile("@return(.*)@examples", re.DOTALL).findall(doc)

    if not full_description:
        full_description = re.compile("@return(.*)##", re.DOTALL).findall(doc)

    if not full_description:
        full_description = re.compile("@return(.*)$", re.DOTALL).findall(doc)

    if full_description:
        full_description = full_description[0].strip()

    full_description = sanitize_description(full_description)

    if full_description:
        full_description = re.sub("((.*)-reply)", "", full_description)
        full_description = full_description.split("\n")
        full_description = [k.strip().lstrip(":") for k in full_description]
        full_description = [k.strip() for k in full_description if k.strip()]
    collection_type = Tuple
    if return_description:
        if len(return_description) > 0:
            rtypes = {k[1]: k[2].replace("@examples", "") for k in return_description}
            has_nil = False
            has_bool = False

            if "simple-string" in rtypes and (
                True
                or rtypes["simple-string"].find("OK") >= 0
                or rtypes["simple-string"].find("an error") >= 0
                or not rtypes["simple-string"].strip()
            ):
                has_bool = True
                rtypes.pop("simple-string")

            if "nil" in rtypes:
                rtypes.pop("nil")
                has_nil = True

            for t, description in list(rtypes.items()):
                if (
                    "`nil`" in description
                    or "`null`" in description
                    or "`NULL`" in description
                    or "`None`" in description
                    or "`none`" in description
                ) and "or" in description:
                    has_nil = True
                elif (
                    "`nil`" in description
                    or "`null`" in description
                    or "`NULL`" in description
                    or "`None`" in description
                    or "`none`" in description
                ):
                    has_nil = True
                    rtypes.pop(t)

            full_description_joined = "\n".join(full_description)

            if (
                "`nil`" in full_description_joined
                or "`null`" in full_description_joined
                or "`NULL`" in full_description_joined
                or "`None`" in full_description_joined
                or "`none`" in full_description_joined
            ):
                has_nil = True

            mapped_types = {
                k: REDIS_RETURN_ARGUMENT_TYPE_MAPPING.get(k, "Any") for k in rtypes
            }
            # special handling for special types

            if "array" in mapped_types:
                if group == "list":
                    collection_type = mapped_types["array"] = List

                if group == "set":
                    collection_type = mapped_types["array"] = Set

            if has_bool:
                mapped_types["bool"] = bool

            if len(mapped_types) > 1:
                if "array" in mapped_types:
                    if (
                        rtypes.get("bulk-string", "").find("floating point") >= 0
                        or rtypes.get("bulk-string", "").find("double precision") >= 0
                        or rtypes.get("bulk-string", "").find("a double") >= 0
                    ):
                        mapped_types["array"] = (
                            Tuple[float, ...]
                            if collection_type == Tuple
                            else collection_type[float]
                        )
                    elif rtypes["array"].find("elements") >= 0:
                        mapped_types["array"] = (
                            Tuple[AnyStr, ...]
                            if collection_type == Tuple
                            else collection_type[AnyStr]
                        )
                    else:
                        mapped_types["array"] = (
                            Tuple[AnyStr, ...]
                            if collection_type == Tuple
                            else collection_type[AnyStr]
                        )

                if "bulk-string" in mapped_types:
                    if (
                        rtypes.get("bulk-string", "").find("floating point") >= 0
                        or rtypes.get("bulk-string", "").find("double precision") >= 0
                        or rtypes.get("bulk-string", "").find("a double") >= 0
                    ):
                        mapped_types["bulk-string"] = float

                    if rtypes.get("bulk-string", "").lower().find("an error") >= 0:
                        mapped_types.pop("bulk-string")
                        rtypes.pop("bulk-string")

                rtype = (
                    Optional[Union[tuple(mapped_types.values())]]
                    if has_nil
                    else Union[tuple(mapped_types.values())]
                )
            else:
                return_details = "\n".join(full_description)
                sub_type = list(mapped_types.values())[0]

                if "array" in rtypes:
                    sub_type_nil = "nil" in rtypes["array"]

                    if rtypes["array"].find("nested") >= 0:
                        sub_type = sub_type[sub_type[Any]]
                    else:
                        if sub_type == Tuple:
                            if rtypes["array"].find("integer") >= 0:
                                sub_type = (
                                    sub_type[int, ...]
                                    if not sub_type_nil
                                    else sub_type[Optional[int], ...]
                                )
                            elif rtypes["array"].find("and their") >= 0:
                                sub_type = Dict[AnyStr, AnyStr]
                            elif rtypes["array"].find("a double") >= 0:
                                sub_type = (
                                    sub_type[float, ...]
                                    if not sub_type_nil
                                    else sub_type[Optional[float], ...]
                                )
                            else:
                                sub_type = (
                                    sub_type[AnyStr, ...]
                                    if not sub_type_nil
                                    else sub_type[Optional[AnyStr], ...]
                                )

                            if sub_type_nil:
                                has_nil = False
                        else:
                            if rtypes["array"].find("integer") >= 0:
                                sub_type = (
                                    sub_type[int]
                                    if not sub_type_nil
                                    else sub_type[Optional[int]]
                                )
                            elif rtypes["array"].find("and their") >= 0:
                                sub_type = (
                                    sub_type[Tuple[AnyStr, AnyStr]]
                                    if not sub_type_nil
                                    else sub_type[Tuple[AnyStr, AnyStr]]
                                )
                            elif rtypes["array"].find("a double") >= 0:
                                sub_type = (
                                    sub_type[float]
                                    if not sub_type_nil
                                    else sub_type[Optional[float]]
                                )
                            else:
                                sub_type = (
                                    sub_type[AnyStr]
                                    if not sub_type_nil
                                    else sub_type[Optional[AnyStr]]
                                )

                            if sub_type_nil:
                                has_nil = False

                if "integer" in rtypes:
                    if (
                        return_details.find("``0``") >= 0
                        and return_details.find("``1``") >= 0
                    ):
                        sub_type = bool

                if "simple-string" in rtypes:
                    if rtypes["simple-string"].find("a double") >= 0:
                        sub_type = float

                if "bulk-string" in rtypes:
                    if rtypes["bulk-string"].find("a double") >= 0:
                        sub_type = float

                rtype = Optional[sub_type] if has_nil else sub_type

            rdesc = [sanitize_description(k[2]) for k in return_description]
            rdesc = [k for k in rdesc if k.strip()]

            return rtype, full_description

    return Any, ""


@functools.lru_cache
def get_official_commands(group=None, include_skipped=False):
    response = get_commands()
    by_group = {}
    [
        by_group.setdefault(command["group"], []).append({**command, **{"name": name}})
        for name, command in response.items()
        if version.parse(command["since"]) < MAX_SUPPORTED_VERSION
        and include_skipped
        or name not in SKIP_COMMANDS
    ]

    return by_group if not group else by_group.get(group)


@functools.lru_cache
def get_module_commands(module: str):
    response = get_commands(module + ".json")
    by_module = {}
    [
        by_module.setdefault(command["group"], []).append(
            {**command, **{"name": name, "module": MODULES[module]["module"]}}
        )
        for name, command in response.items()
        if command["group"] == MODULES[module]["group"]
    ]
    return by_module


def find_method(kls, command_name):
    members = inspect.getmembers(kls)
    mapping = {
        k[0]: k[1]
        for k in members
        if inspect.ismethod(k[1]) or inspect.isfunction(k[1])
    }

    return mapping.get(command_name)


def compare_methods(l, r):
    _l = l
    wrapped = getattr(l, "__wrapped__", None)

    while wrapped:
        _l = _l.__wrapped__
        wrapped = getattr(_l, "__wrapped__", None)
    _r = r
    wrapped = getattr(r, "__wrapped__", None)

    while wrapped:
        _r = _r.__wrapped__
        wrapped = getattr(_r, "__wrapped__", None)

    return _l == _r


def redis_command_link(command):
    return (
        f'`{command} <https://redis.io/commands/{command.lower().replace(" ", "-")}>`_'
    )


def skip_command(command):
    if (
        command["name"].find(" HELP") >= 0
        or command["summary"].find("container for") >= 0
    ):
        return True

    return False


def is_deprecated(command, kls):
    if command.get("deprecated_since"):
        replacement = command.get("replaced_by", "")
        replacement = re.sub("`(.*?)`", "``\\1``", replacement)
        replacement_tokens = [k for k in re.findall("(``(.*?)``)", replacement)]
        replacement_string = {}
        all_commands = get_commands()

        for token in replacement_tokens:
            if token[1] in all_commands:
                replacement_string[
                    token[0]
                ] = f":meth:`~coredis.{kls.__name__}.{sanitized(token[1], None, ignore_reserved_words=True)}`"
            else:
                replacement_string[token[1]] = sanitized(
                    token[1], None, ignore_reserved_words=True
                )

        for token, mapped in replacement_string.items():
            replacement = replacement.replace(token, mapped)

        return version.parse(command["deprecated_since"]), replacement
    else:
        return [None, None]


def sanitized(x, command=None, ignore_reserved_words=False):
    cleansed_name = (
        x.lower()
        .strip()
        .replace("-", "_")
        .replace(":", "_")
        .replace(" ", "_")
        .replace(".", "_")
    )
    cleansed_name = re.sub("[!=><\(\),]", "_", cleansed_name)
    if command:
        override = REDIS_ARGUMENT_NAME_OVERRIDES.get(command["name"], {}).get(
            cleansed_name
        )

        if override:
            cleansed_name = override

    if cleansed_name == "id":
        return "identifier"

    if not ignore_reserved_words and cleansed_name in (
        list(globals()["__builtins__"].__dict__.keys())
        + ["async", "return", "if", "else", "for"]
    ):
        cleansed_name = cleansed_name + "_"

    return cleansed_name


def skip_arg(argument, command):
    arg_version = argument.get("since")

    if arg_version and version.parse(arg_version) > MAX_SUPPORTED_VERSION:
        return True

    if argument["name"] in IGNORED_ARGUMENTS.get(command["name"], []):
        return True

    return False


def relevant_min_version(v, min=True):
    if not v:
        return False

    if min:
        return version.parse(v) > MIN_SUPPORTED_VERSION
    else:
        return version.parse(v) <= MAX_SUPPORTED_VERSION


def get_type(arg, command):
    inferred_type = REDIS_ARGUMENT_TYPE_MAPPING.get(arg["type"], Any)
    sanitized_name = sanitized(arg["name"])
    command_arg_overrides = REDIS_ARGUMENT_TYPE_OVERRIDES.get(command["name"], {})

    if arg_type_override := command_arg_overrides.get(
        arg["name"], command_arg_overrides.get(sanitized_name)
    ):
        return arg_type_override

    if arg["name"] in ["seconds", "milliseconds"] and inferred_type == int:
        return Union[int, datetime.timedelta]

    if arg["name"] == "yes/no" and inferred_type in [StringT, ValueT]:
        return bool

    if (
        arg["name"]
        in [
            "value",
            "element",
            "pivot",
            "member",
            "id",
            "min",
            "max",
            "start",
            "end",
            "argument",
            "arg",
            "port",
        ]
        and inferred_type == StringT
    ):
        return ValueT

    return inferred_type


def get_type_annotation(arg, command, parent=None, default=None):
    if arg["type"] == "oneof" and all(
        k["type"] == "pure-token" for k in arg["arguments"]
    ):
        tokens = [
            "PureToken.%s" % sanitized(s["name"], ignore_reserved_words=True).upper()
            for s in arg["arguments"]
        ]
        literal_type = eval(f"Literal[{','.join(sorted(tokens))}]")

        if (
            arg.get("optional")
            and default is None
            or (parent and (parent.get("optional") or parent.get("partof") == "oneof"))
        ):
            return Optional[literal_type]

        return literal_type
    else:
        return get_type(arg, command)


def get_argument(
    arg,
    parent,
    command,
    arg_type=inspect.Parameter.KEYWORD_ONLY,
    multiple=False,
    num_multiples=0,
):
    if skip_arg(arg, command):
        return [[], [], {}]
    min_version = arg.get("since", None)

    param_list = []
    decorators = []
    meta_mapping = {}

    if arg["type"] == "block":
        if arg.get("multiple") or all(
            c.get("multiple") for c in arg.get("arguments", [])
        ):
            name = sanitized(arg["name"], command)
            if not inflection_engine.singular_noun(name) or name == "prefix":
                name = inflection_engine.plural(name)
            forced_order = BLOCK_ARGUMENT_FORCED_ORDER.get(command["name"], {}).get(
                name
            )
            if forced_order:
                child_args = sorted(
                    arg["arguments"], key=lambda a: forced_order.index(a["name"])
                )
            else:
                child_args = arg["arguments"]
            child_types = [get_type(child, command) for child in child_args]

            for c in child_args:
                if relevant_min_version(c.get("since", None)):
                    meta_mapping.setdefault(c["name"], {})["version"] = c.get(
                        "since", None
                    )

            if arg_type_override := REDIS_ARGUMENT_TYPE_OVERRIDES.get(
                command["name"], {}
            ).get(name):
                annotation = arg_type_override
            else:
                if len(child_types) == 1:
                    annotation = Parameters[child_types[0]]
                elif len(child_types) == 2 and child_types[0] in [StringT, ValueT]:
                    annotation = Dict[child_types[0], child_types[1]]
                else:
                    child_types_repr = ",".join(
                        [
                            "%s" % k if hasattr(k, "_name") else k.__name__
                            for k in child_types
                        ]
                    )
                    annotation = Parameters[eval(f"Tuple[{child_types_repr}]")]

            extra = {}

            if (
                arg.get("optional")
                or (parent and parent.get("optional"))
                or (parent and parent.get("partof") == "oneof")
            ):
                extra["default"] = ARGUMENT_DEFAULTS.get(command["name"], {}).get(name)

                if extra.get("default") is None:
                    annotation = Optional[annotation]
            param_list.append(
                inspect.Parameter(name, arg_type, annotation=annotation, **extra)
            )

            if relevant_min_version(arg.get("since", None)):
                meta_mapping.setdefault(name, {})["version"] = arg.get("since", None)

        else:
            plist_d = []
            children = sorted(
                arg["arguments"], key=lambda v: int(v.get("optional") == True)
            )
            for child in list(children):
                if child["type"] == "pure-token" and not "optional" in child:
                    children.remove(child)
                elif child["name"] == "count" and "token" in child:
                    children.remove(child)
            if len(children) == 1 and not arg.get("multiple"):
                a = children[0].copy()
                if parent:
                    a["name"] = parent["name"] + "_" + arg["name"]
                else:
                    a["name"] = arg["name"]

                plist_p, declist, vmap = get_argument(
                    a, parent, command, arg_type, a.get("multiple", False)
                )
                param_list.extend(plist_p)
                meta_mapping.update(vmap)
            if parent and parent.get("type") == "oneof":
                arg["partof"] = "oneof"
            for child in children:
                plist, declist, vmap = get_argument(
                    child, arg, command, arg_type, arg.get("multiple"), num_multiples
                )
                param_list.extend(plist)
                meta_mapping.update(vmap)
                if not child.get("optional"):
                    plist_d.extend(plist)

            if len(plist_d) > 1:
                mutually_inclusive_params = ",".join(
                    ["'%s'" % child.name for child in plist_d]
                )
                decorators.append(
                    f"@mutually_inclusive_parameters({mutually_inclusive_params})"
                )

    elif arg["type"] == "oneof":
        extra_params = {}

        if all(child["type"] == "pure-token" for child in arg["arguments"]):
            if parent:
                syn_name = sanitized(f"{parent['name']}_{arg.get('name')}", command)
            else:
                syn_name = sanitized(f"{arg.get('token', arg.get('name'))}", command)

            if (
                arg.get("optional")
                or parent
                and parent.get("optional")
                or parent
                and parent.get("partof") == "oneof"
            ):
                extra_params["default"] = ARGUMENT_DEFAULTS.get(
                    command["name"], {}
                ).get(syn_name)
            param_list.append(
                inspect.Parameter(
                    syn_name,
                    arg_type,
                    annotation=get_type_annotation(
                        arg, command, parent, default=extra_params.get("default")
                    ),
                    **extra_params,
                )
            )

            if relevant_min_version(arg.get("since", None)):
                meta_mapping.setdefault(syn_name, {})["version"] = arg.get(
                    "since", None
                )
        else:
            plist_d = []

            for child in arg["arguments"]:
                plist, declist, vmap = get_argument(
                    child, arg, command, arg_type, multiple, num_multiples
                )
                param_list.extend(plist)
                plist_d.extend(plist)
                meta_mapping.update(vmap)

            if len(plist_d) > 1:
                mutually_exclusive_params = ",".join(["'%s'" % p.name for p in plist_d])
                decorators.append(
                    f"@mutually_exclusive_parameters({mutually_exclusive_params})"
                )
    else:
        name = sanitized(
            arg.get("token", arg["name"])
            if not arg.get("type") == "pure-token"
            else arg["name"],
            command,
        )
        type_annotation = get_type_annotation(
            arg,
            command,
            parent,
            default=ARGUMENT_DEFAULTS.get(command["name"], {}).get(name),
        )
        extra_params = {}

        if parent and (
            parent.get("optional")
            or parent.get("type") == "oneof"
            or parent.get("partof") == "oneof"
        ):
            type_annotation = Optional[type_annotation]
            extra_params = {"default": None}

        if is_arg_optional(arg, command, parent) and not arg.get("multiple"):
            type_annotation = Optional[type_annotation]
            extra_params = {"default": None}
        else:
            default = ARGUMENT_DEFAULTS_NON_OPTIONAL.get(command["name"], {}).get(name)

            if default is not None:
                extra_params["default"] = default
                arg_type = inspect.Parameter.KEYWORD_ONLY

        if multiple:
            name = inflection_engine.plural(name)

            if not inflection_engine.singular_noun(name):
                name = inflection_engine.plural(name)
            is_variadic = arg.get("optional") and num_multiples <= 1
            forced_variadicity = ARGUMENT_VARIADICITY.get(command["name"], {}).get(
                name, None
            )

            if forced_variadicity is not None:
                is_variadic = forced_variadicity

            if not is_variadic:
                if (
                    default := ARGUMENT_DEFAULTS.get(command["name"], {}).get(name)
                ) is not None:
                    type_annotation = Parameters[type_annotation]
                    extra_params["default"] = default
                elif (
                    is_arg_optional(arg, command, parent)
                    and extra_params.get("default") is None
                ):
                    type_annotation = Optional[Parameters[type_annotation]]
                    extra_params["default"] = None
                else:
                    type_annotation = Parameters[type_annotation]
            else:
                arg_type = inspect.Parameter.VAR_POSITIONAL

        if "default" in extra_params:
            extra_params["default"] = ARGUMENT_DEFAULTS.get(command["name"], {}).get(
                name, extra_params.get("default")
            )

        if relevant_min_version(min_version):
            meta_mapping.setdefault(name, {})["version"] = min_version
        else:
            if parent:
                if relevant_min_version(parent.get("since", None)):
                    meta_mapping.setdefault(name, {})["version"] = parent.get("since")

        if not multiple:
            if parent and parent.get("token"):
                meta_mapping.setdefault(name, {}).update(
                    {"prefix_token": parent.get("token")}
                )
        else:
            if arg.get("token"):
                meta_mapping.setdefault(name, {}).update(
                    {"prefix_token": arg.get("token")}
                )

        param_list.append(
            inspect.Parameter(
                name, arg_type, annotation=type_annotation, **extra_params
            )
        )

    return [param_list, decorators, meta_mapping]


def is_arg_optional(arg, command, parent=None):
    command_optionality = ARGUMENT_OPTIONALITY.get(command["name"], {})
    override = command_optionality.get(
        sanitized(arg.get("name", ""), command)
    ) or command_optionality.get(sanitized(arg.get("token", ""), command))

    if override is not None:
        return override

    return (
        arg.get("optional")
        or (parent and parent.get("optional"))
        or (parent and parent.get("partof") == "oneof")
    )


def get_command_spec(command):
    arguments = command.get("arguments", []) + REDIS_ARGUMENT_FORCED.get(
        command["name"], []
    )
    arguments = [
        a
        for a in arguments
        if not (a["type"] == "pure-token" and not {"optional", "multiple"} & a.keys())
    ]
    recommended_signature = []
    decorators = []
    forced_order = REDIS_ARGUMENT_FORCED_ORDER.get(command["name"], [])
    mapping = {}
    meta_mapping = {}
    arg_names = [k["name"] for k in arguments]
    initial_order = [(k["name"], k.get("token", "")) for k in arguments]
    history = command.get("history", [])
    extra_version_info = {}
    num_multiples = len([k for k in arguments if k.get("multiple")])
    for arg_name in arg_names:
        for version, entry in history:
            if "`%s`" % arg_name in entry and "added" in entry.lower():
                extra_version_info[arg_name] = version

    for k in arguments:
        version_added = extra_version_info.get(k["name"])

        if version_added and not k.get("since"):
            k["since"] = version_added

    for k in arguments:
        if not is_arg_optional(k, command) and not k.get("multiple"):
            plist, dlist, vmap = get_argument(
                k,
                None,
                command,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                num_multiples=num_multiples,
            )
            mapping[(k["name"], k.get("token", ""))] = (k, plist)
            recommended_signature.extend(plist)
            decorators.extend(dlist)
            meta_mapping.update(vmap)

    for k in arguments:
        if not is_arg_optional(k, command) and k.get("multiple"):
            plist, dlist, vmap = get_argument(
                k,
                None,
                command,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                True,
                num_multiples=num_multiples,
            )
            mapping[(k["name"], k.get("token", ""))] = (k, plist)
            recommended_signature.extend(plist)
            decorators.extend(dlist)
            meta_mapping.update(vmap)

    var_args = [
        k.name
        for k in recommended_signature
        if k.kind == inspect.Parameter.VAR_POSITIONAL
    ]

    if forced_order:
        recommended_signature = sorted(
            recommended_signature,
            key=lambda r: forced_order.index(r.name)
            if r.name in forced_order
            else recommended_signature.index(r),
        )

    if not var_args and not forced_order:
        recommended_signature = sorted(
            recommended_signature,
            key=lambda r: -5
            if r.name in ["key", "keys"]
            else -4
            if r.name in ["arg", "args"]
            else -3
            if r.name == "weights"
            else -2
            if r.name == "start" and "end" in arg_names
            else -1
            if r.name == "end" and "start" in arg_names
            else recommended_signature.index(r),
        )

        for idx, k in enumerate(recommended_signature):
            if k.name == "key":
                n = inspect.Parameter(
                    k.name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=k.default,
                    annotation=k.annotation,
                )
                recommended_signature.remove(k)
                recommended_signature.insert(idx, n)

    elif {"key"} & {r.name for r in recommended_signature} and not forced_order:
        new_recommended_signature = sorted(
            recommended_signature,
            key=lambda r: -1 if r.name in ["key"] else recommended_signature.index(r),
        )
        reordered = [k.name for k in new_recommended_signature] != [
            k.name for k in recommended_signature
        ]

        for idx, k in enumerate(new_recommended_signature):
            if reordered:
                if k.kind == inspect.Parameter.VAR_POSITIONAL:
                    n = inspect.Parameter(
                        k.name,
                        inspect.Parameter.KEYWORD_ONLY,
                        default=k.default,
                        annotation=Parameters[k.annotation],
                    )
                    new_recommended_signature.remove(k)
                    new_recommended_signature.insert(idx, n)

            if k.name == "key":
                n = inspect.Parameter(
                    k.name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=k.default,
                    annotation=k.annotation,
                )
                new_recommended_signature.remove(k)
                new_recommended_signature.insert(idx, n)
            recommended_signature = new_recommended_signature

    for k in sorted(arguments, key=lambda r: -1 if r["type"] == "oneof" else 0):
        if is_arg_optional(k, command) and k.get("multiple"):
            plist, dlist, vmap = get_argument(
                k,
                None,
                command,
                inspect.Parameter.KEYWORD_ONLY,
                True,
                num_multiples=num_multiples,
            )
            mapping[(k["name"], k.get("token", ""))] = (k, plist)
            recommended_signature.extend(plist)
            decorators.extend(dlist)
            meta_mapping.update(vmap)

    remaining = [
        k for k in arguments if is_arg_optional(k, command) and not k.get("multiple")
    ]
    remaining_signature = []

    for k in remaining:
        if skip_arg(k, command):
            continue
        plist, dlist, vmap = get_argument(k, None, command, num_multiples=num_multiples)
        mapping[(k["name"], k.get("token", ""))] = (k, plist)
        remaining_signature.extend(plist)
        decorators.extend(dlist)
        meta_mapping.update(vmap)

    if not forced_order:
        remaining_signature = sorted(
            remaining_signature,
            key=lambda s: -1
            if s.name in ["identifier", "identifiers"]
            else remaining_signature.index(s),
        )
    recommended_signature.extend(remaining_signature)

    if (
        len(recommended_signature) > 1
        and recommended_signature[-2].kind == inspect.Parameter.POSITIONAL_ONLY
    ):
        recommended_signature[-1] = inspect.Parameter(
            recommended_signature[-1].name,
            inspect.Parameter.POSITIONAL_ONLY,
            default=recommended_signature[-1].default,
            annotation=recommended_signature[-1].annotation,
        )

    if forced_order:
        recommended_signature = sorted(
            recommended_signature,
            key=lambda r: forced_order.index(r.name)
            if r.name in forced_order
            else recommended_signature.index(r),
        )

    mapping = OrderedDict(
        {
            k: v
            for k, v in sorted(
                mapping.items(), key=lambda tup: initial_order.index(tup[0])
            )
        }
    )

    return recommended_signature, decorators, mapping, meta_mapping


def generate_method_details(kls, method, module=None, debug=False):
    method_details = {"kls": kls, "command": method}

    if skip_command(method):
        return method_details
    name = MAPPING.get(
        method["name"],
        method["name"].strip().lower().replace(" ", "_").replace("-", "_"),
    )
    if module:
        name = name.replace(MODULES[module].get("prefix", module) + ".", "")
    method_details["name"] = name
    method_details["redis_method"] = method
    method_details["located"] = find_method(kls, name)
    method_details["deprecation_info"] = is_deprecated(method, kls)

    version_introduced = version.parse(method["since"])

    if version_introduced > MIN_SUPPORTED_VERSION or module:
        method_details["redis_version_introduced"] = version_introduced
    else:
        method_details["redis_version_introduced"] = None
    method_details["summary"] = method["summary"]
    return_summary = ""

    if debug and not method["name"] in SKIP_SPEC:
        recommended_return = read_command_docs(
            method["name"], method["group"], module=module
        )

        if recommended_return:
            return_summary = recommended_return[1]
        rec_params, rec_decorators, arg_mapping, meta_mapping = get_command_spec(method)
        seen = set()
        for param in list(rec_params):
            if param.name in seen:
                rec_params.remove(param)
            seen.add(param.name)

        method_details["arg_mapping"] = arg_mapping
        method_details["arg_meta_mapping"] = meta_mapping
        method_details["rec_decorators"] = rec_decorators
        method_details["rec_params"] = rec_params
        try:
            if (
                REDIS_RETURN_OVERRIDES.get(method["name"], None)
                == recommended_return[0]
            ):
                print(f"{method['name']} doesn't need a return override")
            rec_signature = inspect.Signature(
                [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
                + rec_params,
                return_annotation=REDIS_RETURN_OVERRIDES.get(
                    method["name"], recommended_return[0]
                )
                if recommended_return
                else None,
            )
            method_details["rec_signature"] = rec_signature
        except:
            import pdb

            pdb.set_trace()
            raise Exception(method["name"], [(k.name, k.kind) for k in rec_params])

        method_details["readonly"] = READONLY_OVERRIDES.get(
            method["name"], "readonly" in method.get("command_flags", [])
        )
        method_details["return_summary"] = return_summary

    return method_details


def generate_compatibility_section(
    kls,
    parent_kls,
    groups,
    debug=False,
    next_version="6.6.6",
):
    env = Environment()
    section_template_str = """
{% for group in groups %}
{% if group in methods_by_group %}
{{group.title()}}
{{len(group)*'^'}}

{% for method in methods_by_group[group]["supported"] %}
{% set command_link=redis_command_link(method['redis_method']['name']) -%}
{% if debug %}
{{ method['redis_method']['name'] }} {% if method["full_match"] -%}[✓]{% endif %}
{% else %}
{{ method['redis_method']['name'] }}
{% endif -%}
{{(len(method['redis_method']['name'])+(4 if debug and method["full_match"] else 0))*"*"}}

{{ method['summary'] }}

- Documentation: {{command_link}}
{% if method["redirect_usage"] and method["redirect_usage"].warn -%}
- Implementation: :meth:`~coredis.{{kls.__name__}}.{{method["located"].__name__}}`

  .. warning:: Using :meth:`~coredis.{{kls.__name__}}.{{method["located"].__name__}}` directly is not recommended. {{method["redirect_usage"].reason}}
{% elif method["redirect_usage"] and not method["redirect_usage"].warn -%}
- .. danger:: :meth:`~coredis.{{kls.__name__}}.{{method["located"].__name__}}` intentionally raises an :exc:`NotImplemented` error. {{method["redirect_usage"].reason}}
{% else -%}
- Implementation: :meth:`~coredis.{{kls.__name__}}.{{method["located"].__name__}}`
{% endif -%}
{% if method["redis_version_introduced"] and method["redis_version_introduced"] > MIN_SUPPORTED_VERSION %}
- New in redis: {{method["redis_version_introduced"]}}
{% endif %}
{% if method["deprecation_info"][0] %}
- Deprecated in redis: {{method["deprecation_info"][0] }}. Use {{method["deprecation_info"][1]}}
{% endif %}
{% if method["version_added"] %}
- {{method["version_added"]}}
{% endif %}
{% if method["version_changed"] %}
- {{method["version_changed"]}}
{% endif %}
{% if method["located"] and getattr(method["located"], "__coredis_command", None) and method["located"].__coredis_command.cache_config %}
- Supports client caching: yes
{% endif -%}
{% if debug %}
- Current Signature {% if method.get("full_match") %} (Full Match) {% else %} ({{method["mismatch_reason"]}}) {% endif %}

  .. code::

     {% for decorator in method["rec_decorators"] -%}
     {{decorator}}
     {% endfor -%}
     {% if not method["full_match"] -%}
     @redis_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}
     {%- if method["redis_version_introduced"] and method["redis_version_introduced"] > MIN_SUPPORTED_VERSION -%}
     , version_introduced="{{method["command"].get("since")}}"
     {%- endif -%}
     {%- if method["deprecation_info"][0] and method["deprecation_info"][0] >= MIN_SUPPORTED_VERSION -%}
     , version_deprecated="{{method["command"].get("deprecated_since")}}"
     {%- endif -%}, group=CommandGroup.{{method["command"]["group"].upper().replace(" ", "_").replace("-", "_")}}
     {%- if len(method["arg_meta_mapping"]) > 0 -%}
     {% set argument_with_version = {} %}
     {%- for name, meta  in method["arg_meta_mapping"].items() -%}
     {%- if meta and meta["version"] and version_parse(meta["version"]) >= MIN_SUPPORTED_VERSION -%}
     {% set _ = argument_with_version.update({name: {"version_introduced": meta["version"]}}) %}
     {%- endif -%}
     {%- endfor -%}
     {% if method["readonly"] %}, readonly=True{% endif -%}
     {% if argument_with_version %}, arguments={{ argument_with_version }}{% endif %}
     {%- endif -%})
     {%- endif -%}
     {% set implementation = method["located"] -%}
     {% set implementation = inspect.getclosurevars(implementation).nonlocals.get("func", implementation) -%}
     {% if method["full_match"] %}
     async def {{method["name"]}}{{render_signature(method["current_signature"])}}:
     {% else %}
     - async def {{method["name"]}}{{render_signature(method["current_signature"], True)}}:
     + async def {{method["name"]}}{{render_signature(method["rec_signature"], True)}}:
     {% endif %}
         \"\"\"
         {% for line in (implementation.__doc__ or "").split("\n") -%}
         {{line.lstrip()}}
         {% endfor %}
         {% if method["return_summary"] and not method["located"].__doc__.find(":return:")>=1-%}
         \"\"\"

         \"\"\"
         Recommended docstring:

         {{method["summary"]}}

         {% if method["located"].__doc__.find(":param:") < 0 -%}
         {% for p in list(method["rec_signature"].parameters)[1:] -%}
         {% if p != "key" -%}
         :param {{p}}:
         {%- endif -%}
         {% endfor %}
         {% endif -%}
         {% if len(method["return_summary"]) == 1 -%}
         :return: {{method["return_summary"][0]}}
         {%- else -%}
         :return:
         {% for desc in method["return_summary"] -%}
         {{desc}}
         {%- endfor -%}
         {% endif %}
         {% endif -%}
         \"\"\"
         {% if "execute_command" not in inspect.getclosurevars(implementation).unbound -%}
         {% if len(method["arg_mapping"]) > 0 -%}
         pieces: CommandArgList = []
         {% for name, arg  in method["arg_mapping"].items() -%}

         {% if len(arg[1]) > 0 -%}
         {%- for param in arg[1] -%}
         {%- if not arg[0].get("optional") %}
         {%- if arg[0].get("multiple") %}
         {%- if arg[0].get("token") %}
         pieces.extend(*{{param.name}})
         {%- else %}
         pieces.extend(*{{param.name}})
         {%- endif %}
         {%- else %}
         {%- if arg[0].get("token") %}
         pieces.append("{{arg[0].get("token")}}")
         pieces.append({{param.name}})
         {%- else %}
         pieces.append({{param.name}})
         {%- endif %}
         {%- endif %}
         {%- else %}
         {% if arg[0].get("multiple") -%}

         if {{param.name}}:
         {%- if arg[0].get("multiple_token") %}
             for item in {{param.name}}:
                 pieces.append("{{arg[0].get("token")}}")
                 pieces.append(item)
         {%- else %}
             pieces.append("{{arg[0].get("token")}}")
             pieces.extend({{param.name}})
         {%- endif %}
         {%- else %}

         if {{param.name}}{% if arg[0].get("type") != "pure-token" %} is not None{%endif%}:
         {%- if arg[0].get("token") and arg[0].get("type") != "pure-token" %}
             pieces.append("{{arg[0].get("token")}}")
             pieces.extend({{param.name}})
         {%- else %}
             {%- if arg[0].get("type") == "oneof" %}
             pieces.append({{param.name}})
             {%- else %}
             pieces.append(PureToken.{{arg[0].get("token")}})
             {% endif %}
         {%- endif %}
         {%- endif %}
         {%- endif %}
         {%- endfor %}
         {%- endif %}
         {%- endfor %}

         return await self.execute_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}, *pieces)
         {% else -%}

         return await self.execute_command(CommandName.{{sanitized(method["command"]["name"]).upper()}})
         {% endif -%}
         {% endif -%}
{% endif %}
{% endfor %}
{% for method in methods_by_group[group]["missing"] %}
{% set command_link= redis_command_link(method['redis_method']['name']) -%}
{{ method["command"]["name"] }} [X]
{{(len(method["command"]["name"])+4)*"*"}}

{{ method['summary'] }}

- Documentation: {{ command_link }}
{% if debug %}
- Recommended Signature:

  .. code::

     {% for decorator in method["rec_decorators"] %}
     {{decorator}}
     {% endfor -%}
     @versionadded(version="{{next_version}}")
     @redis_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}
     {%- if method["redis_version_introduced"] and method["redis_version_introduced"] > MIN_SUPPORTED_VERSION -%}
     , version_introduced="{{method["command"].get("since")}}"
     {%- endif -%}, group=CommandGroup.{{method["command"]["group"].upper().replace(" ","_").replace("-","_")}}
     {%- if len(method["arg_mapping"]) > 0 -%}
     {% set argument_with_version = {} %}
     {%- for name, arg  in method["arg_mapping"].items() -%}
     {%- for param in arg[1] -%}
     {%- if arg[0].get("since") and version_parse(arg[0].get("since")) >= MIN_SUPPORTED_VERSION -%}
     {% set _ = argument_with_version.update({param.name: {"version_introduced": arg[0].get("since")}}) %}
     {%- endif -%}
     {%- endfor -%}
     {%- endfor -%}
     {% if method["readonly"] %}, readonly=True{% endif -%}
     {% if argument_with_version %}, arguments={{ argument_with_version }}{%endif%}
     {%- endif -%})
     async def {{method["name"]}}{{render_signature(method["rec_signature"])}}:
         \"\"\"
         {{method["summary"]}}

         {% if "rec_signature" in method %}
         {% for p in list(method["rec_signature"].parameters)[1:] %}
         :param {{p}}:
         {%- endfor %}
         {% endif %}
         {% if len(method["return_summary"]) == 0 %}
         :return: {{method["return_summary"][0]}}
         {% else %}
         :return:
         {% for desc in method["return_summary"] %}
         {{desc}}
         {%- endfor %}
         {% endif %}
         \"\"\"
         {% if len(method["arg_mapping"]) > 0 -%}
         pieces = []
         {%- for name, arg  in method["arg_mapping"].items() %}
         # Handle {{name}}
         {% if len(arg[1]) > 0 -%}
         {% for param in arg[1] -%}
         {% if not arg[0].get("optional") -%}
         {% if arg[0].get("multiple") -%}
         {% if arg[0].get("token") -%}
         pieces.extend(*{{param.name}})
         {% else -%}
         pieces.extend(*{{param.name}})
         {% endif -%}
         {% else -%}
         {%- if arg[0].get("token") %}
         pieces.append("{{arg[0].get("token")}}")
         pieces.append({{param.name}})
         {% else -%}
         pieces.append({{param.name}})
         {% endif -%}
         {% endif -%}
         {% else -%}
         {% if arg[0].get("multiple") %}

         if {{arg[1][0].name}}:
            pieces.extend({{param.name}})
         {% else %}

         if {{param.name}}{% if arg[0].get("type") != "pure-token" %} is not None{%endif%}:
         {%- if arg[0].get("token")  and arg[0].get("type") == "oneof" %}
            pieces.append({{param.name}}.value)
         {%- else %}
            pieces.extend(["{{arg[0].get("token")}}", {{param.name}}])
         {% endif -%}
         {% endif -%}
         {% endif %}
         {% endfor -%}
         {% endif -%}
         {% endfor -%}

         return await self.execute_command(CommandName.({{sanitized(method["command"]["name"]).upper()}}, *pieces)
         {% else -%}

        return await self.execute_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}")
        {% endif -%}
{% else %}
- Not Implemented
{% endif %}
{% endfor %}
{% endif %}
{% endfor %}

    """
    env.globals.update(
        MIN_SUPPORTED_VERSION=MIN_SUPPORTED_VERSION,
        MAX_SUPPORTED_VERSION=MAX_SUPPORTED_VERSION,
        get_official_commands=get_official_commands,
        inspect=inspect,
        len=len,
        list=list,
        version_parse=version.parse,
        skip_command=skip_command,
        redis_command_link=redis_command_link,
        find_method=find_method,
        kls=kls,
        render_signature=render_signature,
        next_version=next_version,
        debug=debug,
        sanitized=sanitized,
        getattr=getattr,
    )
    section_template = env.from_string(section_template_str)
    methods_by_group = {}

    for group in groups:
        methods = {"supported": [], "missing": []}
        for method in get_official_commands(group):
            method_details = generate_method_details(kls, method, debug=debug)
            if debug and not method_details.get("rec_signature"):
                continue
            if not debug and skip_command(method):
                continue

            located = method_details.get("located")

            if (
                parent_kls
                and located
                and compare_methods(
                    find_method(parent_kls, sanitized(method["name"])), located
                )
            ):
                continue
            if located:
                version_added = VERSIONADDED_DOC.findall(located.__doc__)
                version_added = (version_added and version_added[0][0]) or ""
                version_added.strip()

                version_changed = VERSIONCHANGED_DOC.findall(located.__doc__)
                version_changed = (version_changed and version_changed[0][0]) or ""
                method_details["version_changed"] = version_changed
                method_details["version_added"] = version_added

                command_details = getattr(located, "__coredis_command", None)
                if not method["name"] in SKIP_SPEC:
                    cur = inspect.signature(located)
                    current_signature = [k for k in cur.parameters]
                    method_details["current_signature"] = cur
                    if command_details:
                        method_details[
                            "redirect_usage"
                        ] = command_details.redirect_usage
                    if debug:
                        if (
                            compare_signatures(
                                cur, method_details["rec_signature"], with_return=False
                            )
                            and render_annotation(cur.return_annotation).lower()
                            == render_annotation(
                                method_details["rec_signature"].return_annotation
                            ).lower()
                        ):
                            src = inspect.getsource(located)
                            version_introduced_valid = command_details and str(
                                command_details.version_introduced
                            ) == str(method_details["redis_version_introduced"])
                            version_deprecated_valid = command_details and str(
                                command_details.version_deprecated
                            ) == str(method_details.get("deprecation_info", [None])[0])
                            readonly_valid = (
                                command_details
                                and (CommandFlag.READONLY in command_details.flags)
                                == method_details["readonly"]
                            )
                            arg_version_valid = command_details and len(
                                command_details.arguments
                            ) == len(
                                [
                                    k
                                    for k in method_details["arg_meta_mapping"]
                                    if method_details["arg_meta_mapping"]
                                    .get(k, {})
                                    .get("version")
                                ]
                            )
                            missing_command_flags = []
                            routing_valid = True
                            merging_valid = True
                            if command_details and (hints := method.get("hints", [])):
                                request_policy = [
                                    hint.split("request_policy:")[1]
                                    for hint in hints
                                    if "request_policy" in hint
                                ]
                                response_policy = [
                                    hint.split("response_policy:")[1]
                                    for hint in hints
                                    if "response_policy" in hint
                                ]
                                if request_policy and (
                                    route := command_details.cluster.route
                                ):
                                    routing_valid = (
                                        ROUTE_MAPPING.get(request_policy[0], "")
                                        == route
                                    )
                                    if not routing_valid:
                                        print(
                                            method["name"],
                                            route,
                                            request_policy[0],
                                            ROUTE_MAPPING.get(request_policy[0]),
                                        )
                                if (
                                    response_policy
                                    and command_details.cluster.combine
                                    and (
                                        combine := command_details.cluster.combine.response_policy
                                    )
                                ):
                                    merging_valid = combine in MERGE_MAPPING.get(
                                        response_policy[0], []
                                    )
                            if command_details and (
                                flags := [
                                    k
                                    for k in method.get("command_flags", [])
                                    if k in [e.value for e in CommandFlag]
                                ]
                            ):
                                if set(flags) != set(
                                    k.value for k in command_details.flags
                                ):
                                    missing_command_flags = set(flags) - set(
                                        [k.value for k in command_details.flags]
                                    )
                                method_details["usable_flags"] = set(flags)
                            if (
                                src.find("@redis_command") >= 0
                                and src.find(
                                    method["name"]
                                    .strip()
                                    .upper()
                                    .replace(" ", "_")
                                    .replace("-", "_")
                                )
                                >= 0
                                and command_details
                                and (CommandFlag.READONLY in command_details.flags)
                                == method_details["readonly"]
                                and version_introduced_valid
                                and version_deprecated_valid
                                and arg_version_valid
                                and routing_valid
                                and merging_valid
                                and not missing_command_flags
                            ):
                                method_details["full_match"] = True
                            else:
                                method_details["mismatch_reason"] = (
                                    "Command wrapper missing"
                                    if not command_details
                                    else f"Incorrect version introduced {command_details.version_introduced} vs {method_details['redis_version_introduced']}"
                                    if not version_introduced_valid
                                    else f"Incorrect version deprecated"
                                    if not version_deprecated_valid
                                    else "Readonly flag mismatch"
                                    if not readonly_valid
                                    else "Argument version mismatch"
                                    if not arg_version_valid
                                    else "Routing mismatch"
                                    if not routing_valid
                                    else "Response merge mismatch"
                                    if not merging_valid
                                    else f"Flags mismatched {missing_command_flags}"
                                    if missing_command_flags
                                    else "unknown"
                                )
                        elif compare_signatures(
                            cur, method_details["rec_signature"], with_return=False
                        ):
                            recommended_return = read_command_docs(
                                method["name"], method["group"]
                            )
                            method_details["mismatch_reason"] = "Mismatched return type"
                            if recommended_return:
                                new_sig = inspect.Signature(
                                    [
                                        inspect.Parameter(
                                            "self",
                                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        )
                                    ]
                                    + method_details["rec_params"],
                                    return_annotation=recommended_return[0],
                                )
                        else:
                            method_details["mismatch_reason"] = "Arg mismatch"
                            diff_minus = [
                                str(k)
                                for k, v in method_details[
                                    "rec_signature"
                                ].parameters.items()
                                if k not in current_signature
                            ]
                            diff_plus = [
                                str(k)
                                for k in current_signature
                                if k not in method_details["rec_signature"].parameters
                            ]
                            method_details["diff_minus"] = diff_minus
                            method_details["diff_plus"] = diff_plus
                methods["supported"].append(method_details)
            elif is_deprecated(method, kls) == [None, None]:
                methods["missing"].append(method_details)
        if methods["supported"] or methods["missing"]:
            methods_by_group[group] = methods
    return section_template.render(groups=groups, methods_by_group=methods_by_group)


@click.group()
@click.option("--debug", default=False, help="Output debug")
@click.option("--next-version", default="6.6.6", help="Next version")
@click.pass_context
def code_gen(ctx, debug: bool, next_version: str):
    cur_dir = os.path.split(__file__)[0]
    ctx.ensure_object(dict)
    if debug:
        if not os.path.isdir("/var/tmp/redis-doc"):
            os.system("git clone git@github.com:redis/redis-doc /var/tmp/redis-doc")
        else:
            os.system("cd /var/tmp/redis-doc && git pull")
        shutil.copy("/var/tmp/redis-doc/commands.json", cur_dir)
        for module, details in MODULES.items():
            if not os.path.isdir(f"/var/tmp/redis-module-{details['module']}"):
                os.system(
                    f"git clone {details['repo']} /var/tmp/redis-module-{details['module']}"
                )
            else:
                os.system(f"cd /var/tmp/redis-module-{details['module']} && git pull")
            shutil.copy(
                f"/var/tmp/redis-module-{details['module']}/commands.json",
                os.path.join(cur_dir, f"{module}.json"),
            )

    ctx.obj["DEBUG"] = debug
    ctx.obj["NEXT_VERSION"] = next_version


@code_gen.command()
@click.option("--path", default="docs/source/compatibility.rst")
@click.pass_context
def coverage_doc(ctx, path: str):
    output = f"""
Command compatibility
=====================

This document is generated by parsing the `official redis command documentation <https://redis.io/commands>`_

.. currentmodule:: coredis

"""

    kls = coredis.Redis
    output += generate_compatibility_section(
        kls,
        None,
        STD_GROUPS + ["server", "connection", "cluster"],
        debug=ctx.obj["DEBUG"],
        next_version=ctx.obj["NEXT_VERSION"],
    )
    open(path, "w").write(output)
    print(f"Generated coverage doc at {path}")


@code_gen.command()
@click.option("--path", default="coredis/tokens.py")
@click.pass_context
def token_enum(ctx, path):
    pure_tokens, prefix_tokens = get_token_mapping()

    env = Environment()
    env.globals.update(sorted=sorted)
    t = env.from_string(
        """from __future__ import annotations

from coredis._utils import CaseAndEncodingInsensitiveEnum

class PureToken(CaseAndEncodingInsensitiveEnum):
    \"\"\"
    Enum for using pure-tokens with the redis api.
    \"\"\"

    {% for token, command_usage in pure_tokens.items() -%}

    #: Used by:
    #:
    {% for c in sorted(command_usage) -%}
    #:  - ``{{c}}``
    {% endfor -%}
    {{ token[0].upper().replace("-", "_").replace("/", "_").replace(":", "_") }} = b"{{token[1]}}"

    {% endfor %}
    

class PrefixToken(CaseAndEncodingInsensitiveEnum):
    \"\"\"
    Enum for internal use when adding prefixes to arguments
    \"\"\"

    {% for token, command_usage in prefix_tokens.items() -%}

    #: Used by:
    #:
    {% for c in sorted(command_usage) -%}
    #:  - ``{{c}}``
    {% endfor -%}
    {{ token[0].upper().replace("-", "_").replace("/", "_").replace(":", "_") }} = b"{{token[1]}}"

    {% endfor %}
    """
    )

    result = t.render(pure_tokens=pure_tokens, prefix_tokens=prefix_tokens)
    open(path, "w").write(result)
    format_file_in_place(
        Path(path), fast=False, mode=FileMode(), write_back=WriteBack.YES
    )
    print(f"Generated token enum at {path}")


@code_gen.command()
@click.option("--path", default="coredis/commands/constants.py")
@click.pass_context
def command_constants(ctx, path):
    commands = get_official_commands(include_skipped=True)
    for module in MODULES:
        commands.update(get_module_commands(module))

    sort_fn = lambda command: command.get("since")
    env = Environment()
    env.globals.update(sorted=sorted)
    t = env.from_string(
        """\"\"\"
coredis.commands.constants
--------------------------
Constants relating to redis command names and groups
\"\"\"

from __future__ import annotations

import enum

from coredis._utils import CaseAndEncodingInsensitiveEnum

class CommandName(CaseAndEncodingInsensitiveEnum):
    \"\"\"
    Enum for listing all redis commands
    \"\"\"

    {% for group, commands in commands.items() -%}
    #: Commands for {{group}}
    {% for command in sorted(commands, key=sort_fn) -%}
    {% if not command.get("deprecated_since") -%}
    {% if command.get("since") -%}
    {% if command.get("module") -%}
    {{ command["name"].upper().replace(" ", "_").replace("-", "_").replace(".", "_")}} = b"{{command["name"]}}"  # Since {{command.get("module")}}: {{command["since"]}}
    {% else -%}
    {{ command["name"].upper().replace(" ", "_").replace("-", "_").replace(".", "_")}} = b"{{command["name"]}}"  # Since redis: {{command["since"]}}
    {% endif -%}
    {% else -%}
    {{ command["name"].upper().replace(" ", "_").replace("-", "_").replace(".", "_")}} = b"{{command["name"]}}"
    {% endif -%}
    {% endif -%}
    {% endfor -%}
    {% for command in sorted(commands, key=sort_fn) -%}
    {% if command.get("deprecated_since") -%}
    {% if command.get("module") -%}
    {{ command["name"].upper().replace(" ", "_").replace("-", "_").replace(".", "_")}} = b"{{command["name"]}}"  # Deprecated in {{command.get("module")}}: {{command["deprecated_since"]}}
    {% else -%}
    {{ command["name"].upper().replace(" ", "_").replace("-", "_").replace(".", "_")}} = b"{{command["name"]}}"  # Deprecated in redis: {{command["deprecated_since"]}}
    {% endif -%}
    {% endif -%}
    {% endfor %}
    {% endfor -%}

    #: Oddball command
    DEBUG_OBJECT = b"DEBUG OBJECT"

    #: Sentinel commands
    SENTINEL_CKQUORUM = b"SENTINEL CKQUORUM"
    SENTINEL_CONFIG_GET = b"SENTINEL CONFIG GET"
    SENTINEL_CONFIG_SET = b"SENTINEL CONFIG SET"
    SENTINEL_GET_MASTER_ADDR_BY_NAME = b"SENTINEL GET-MASTER-ADDR-BY-NAME"
    SENTINEL_FAILOVER = b"SENTINEL FAILOVER"
    SENTINEL_FLUSHCONFIG = b"SENTINEL FLUSHCONFIG"
    SENTINEL_INFO_CACHE = b"SENTINEL INFO-CACHE"
    SENTINEL_IS_MASTER_DOWN_BY_ADDR = b"SENTINEL IS-MASTER-DOWN-BY-ADDR"
    SENTINEL_MASTER = b"SENTINEL MASTER"
    SENTINEL_MASTERS = b"SENTINEL MASTERS"
    SENTINEL_MONITOR = b"SENTINEL MONITOR"
    SENTINEL_MYID = b"SENTINEL MYID"
    SENTINEL_PENDING_SCRIPTS = b"SENTINEL PENDING-SCRIPTS"
    SENTINEL_REMOVE = b"SENTINEL REMOVE"
    SENTINEL_SLAVES = b"SENTINEL SLAVES"  # Deprecated
    SENTINEL_REPLICAS = b"SENTINEL REPLICAS"
    SENTINEL_RESET = b"SENTINEL RESET"
    SENTINEL_SENTINELS = b"SENTINEL SENTINELS"
    SENTINEL_SET = b"SENTINEL SET"


class CommandGroup(enum.Enum):
    {% for group in sorted(commands) -%}
    {{ group.upper().replace(" ", "_").replace("-", "_")}} = "{{group.lower()}}"
    {% endfor %}


class NodeFlag(enum.Enum):
    ALL = "all"
    PRIMARIES = "primaries"
    REPLICAS = "replicas"
    RANDOM = "random"
    SLOT_ID = "slot-id"

class CommandFlag(enum.Enum):
    BLOCKING = "blocking"
    SLOW = "slow"
    FAST = "fast"
    READONLY = "readonly"
    """
    )

    result = t.render(commands=commands, sort_fn=sort_fn)
    open(path, "w").write(result)
    format_file_in_place(
        Path(path), fast=False, mode=FileMode(), write_back=WriteBack.YES
    )
    print(f"Generated command enum at {path}")


@code_gen.command()
def changes():
    cur_version = version.parse(coredis.__version__.split("+")[0])
    kls = coredis.Redis
    cluster_kls = coredis.RedisCluster
    new_methods = collections.defaultdict(list)
    changed_methods = collections.defaultdict(list)
    new_cluster_methods = collections.defaultdict(list)
    changed_cluster_methods = collections.defaultdict(list)
    for group in STD_GROUPS + ["server", "connection", "cluster"]:
        for cmd in get_official_commands(group):
            name = MAPPING.get(
                cmd["name"],
                cmd["name"].lower().replace(" ", "_").replace("-", "_"),
            )
            method = find_method(kls, name)
            cluster_method = find_method(cluster_kls, name)
            if method:
                vchanged = version_changed_from_doc(method.__doc__)
                vadded = version_added_from_doc(method.__doc__)
                if vadded and vadded > cur_version:
                    new_methods[group].append(method)
                if vchanged and vchanged > cur_version:
                    changed_methods[group].append(method)
            if cluster_method and not compare_methods(method, cluster_method):
                vchanged = version_changed_from_doc(cluster_method.__doc__)
                vadded = version_added_from_doc(cluster_method.__doc__)
                if vadded and vadded > cur_version:
                    new_cluster_methods[group].append(cluster_method)
                if vchanged and vchanged > cur_version:
                    changed_cluster_methods[group].append(cluster_method)

    print("New APIs:")
    print()
    for group, methods in new_methods.items():
        print(f"    * {group.title()}:")
        print()
        for new_method in sorted(methods, key=lambda m: m.__name__):
            print(f"        * ``{kls.__name__}.{new_method.__name__}``")
        for new_method in sorted(
            new_cluster_methods.get(group, []), key=lambda m: m.__name__
        ):
            print(f"        * ``{cluster_kls.__name__}.{new_method.__name__}``")
        print()
    print()
    print("Changed APIs:")
    print()
    for group, methods in changed_methods.items():
        print(f"    * {group.title()}:")
        print()
        for changed_method in sorted(methods, key=lambda m: m.__name__):
            print(f"        * ``{kls.__name__}.{changed_method.__name__}``")
        for changed_method in sorted(
            changed_cluster_methods.get(group, []), key=lambda m: m.__name__
        ):
            print(f"        * ``{cluster_kls.__name__}.{changed_method.__name__}``")
        print()


@code_gen.command()
@click.option("--command", "-c", multiple=True)
@click.option("--group", "-g", default=None)
@click.option("--module", "-m", default=None)
@click.option("--expr", "-e", default=None)
@click.option("--debug", default=False, help="Output debug")
@click.pass_context
def implementation(ctx, command, group, module, expr, debug=False):
    cur_version = version.parse(coredis.__version__.split("+")[0])
    kls = coredis.Redis

    method_template_str = """
    {% for decorator in method["rec_decorators"] %}
    {{decorator}}
    {% endfor -%}
    {%- if not module %}
    @versionadded(version="{{next_version}}")
    @redis_command("{{method["command"]["name"]}}"
    {%- if method["redis_version_introduced"] and method["redis_version_introduced"] > MIN_SUPPORTED_VERSION -%}
    , version_introduced="{{method["command"].get("since")}}"
    {%- endif -%}, group=CommandGroup.{{method["command"]["group"].upper().replace(" ","_").replace("-","_")}}
    {%- if len(method["arg_mapping"]) > 0 -%}
    {% set argument_with_version = {} %}
    {%- for name, arg  in method["arg_mapping"].items() -%}
    {%- for param in arg[1] -%}
    {%- if arg[0].get("since") and version_parse(arg[0].get("since")) >= MIN_SUPPORTED_VERSION -%}
    {% set _ = argument_with_version.update({param.name: {"version_introduced": arg[0].get("since")}}) %}
    {%- endif -%}
    {%- endfor -%}
    {%- endfor -%}
    {% if argument_with_version %}, arguments={{ argument_with_version }}{%endif%}
    {%- endif -%}
    {%- else -%}
    @module_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}, module="{{module["module"]}}"
    {%- if method["redis_version_introduced"] -%}
    , version_introduced="{{method["command"].get("since")}}"
    {%- endif -%}, group=CommandGroup.{{method["command"]["group"].upper().replace(" ","_").replace("-","_").replace(".", "_")}}
    {%- if len(method["arg_mapping"]) > 0 -%}
    {% set argument_with_version = {} %}
    {%- for name, arg  in method["arg_mapping"].items() -%}
    {%- for param in arg[1] -%}
    {%- if arg[0].get("since")  -%}
    {% set _ = argument_with_version.update({param.name: {"version_introduced": arg[0].get("since")}}) %}
    {%- endif -%}
    {%- endfor -%}
    {%- endfor -%}
    {% if method["readonly"] %}, readonly=True{% endif -%}
    {% if argument_with_version %}, arguments={{ argument_with_version }}{%endif%}
    {%- endif -%}
    {%- endif -%})
    async def {{method["name"]}}{{render_signature(method["rec_signature"], True)}}:
        \"\"\"
        {{method["summary"]}}

        {% if "rec_signature" in method %}
        {% for p in list(method["rec_signature"].parameters)[1:] %}
        :param {{p}}:
        {%- endfor %}
        {% endif %}
        {% if len(method["return_summary"]) == 0 %}
        :return: {{method["return_summary"][0]}}
        {% else %}
        :return:
        {% for desc in method["return_summary"] %}
        {{desc}}
        {%- endfor %}
        {% endif %}
        \"\"\"
        pieces: CommandArgList = []
         
        {% if module %}
        return await self.execute_module_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}, *pieces)
        {% else %} 
        return await self.execute_command(CommandName.{{sanitized(method["command"]["name"]).upper()}}, *pieces)
        {% endif %}
"""
    env = Environment()
    env.globals.update(
        MIN_SUPPORTED_VERSION=MIN_SUPPORTED_VERSION,
        MAX_SUPPORTED_VERSION=MAX_SUPPORTED_VERSION,
        get_official_commands=get_official_commands,
        inspect=inspect,
        len=len,
        list=list,
        version_parse=version.parse,
        skip_command=skip_command,
        redis_command_link=redis_command_link,
        find_method=find_method,
        kls=kls,
        render_signature=render_signature,
        next_version=ctx.obj["NEXT_VERSION"],
        module=MODULES[module] if module else None,
        sanitized=sanitized,
        debug=True,
    )
    method_template = env.from_string(method_template_str)

    if group:
        commands = get_official_commands(group)
    elif module:
        commands = get_module_commands(module)[MODULES[module].get("group", module)]
    else:
        all_commands = get_official_commands()
        commands = []
        for g, group_commands in all_commands.items():
            if expr:
                rex = re.compile(expr)
                commands.extend([k for k in group_commands if rex.match(k["name"])])
            else:
                commands.extend([k for k in group_commands if k["name"] in command])

    for command in commands:
        method_details = generate_method_details(
            kls, command, module=MODULES[module]["module"], debug=True
        )
        if method_details.get("rec_signature"):
            print(method_template.render(method=method_details))


@click.option("--path", default="coredis/pipeline.pyi")
@code_gen.command()
def pipeline_stub(path):
    kls = cluster_kls = coredis.client.Client
    commands = {}

    for group in STD_GROUPS + ["server", "connection", "cluster"]:
        for cmd in get_official_commands(group):
            name = MAPPING.get(
                cmd["name"],
                cmd["name"].lower().replace(" ", "_").replace("-", "_"),
            )
            method = find_method(kls, name)
            cluster_method = find_method(cluster_kls, name)
            if method and hasattr(method, "__coredis_command"):
                signature = inspect.signature(method)
                cluster_signature = inspect.signature(cluster_method)
                commands[cmd["name"]] = {
                    "name": name,
                    "command_details": method.__coredis_command,
                    "pipeline": inspect.Signature(
                        parameters=[
                            k.replace(default=Ellipsis)
                            if k.default is not inspect._empty
                            else k
                            for k in signature.parameters.values()
                        ],
                        return_annotation="Pipeline[AnyStr]",
                    ),
                    "cluster": inspect.Signature(
                        parameters=[
                            k.replace(default=Ellipsis)
                            if k.default is not inspect._empty
                            else k
                            for k in cluster_signature.parameters.values()
                        ],
                        return_annotation="ClusterPipeline[AnyStr]",
                    ),
                }
    stub_template_str = """
from __future__ import annotations

import datetime
from types import TracebackType
from typing import Any

from wrapt import ObjectProxy

from coredis import PureToken
from coredis.client import Client, Redis, RedisCluster
from coredis.commands.script import Script
from coredis.pool import ClusterConnectionPool, ConnectionPool
from coredis.typing import (
    AnyStr,
    Callable,
    Dict,
    Generic,
    Iterable,
    KeyT,
    List,
    Literal,
    Mapping,
    Node,
    Optional,
    Parameters,
    Set,
    StringT,
    Tuple,
    Type,
    Union,
    ValueT,
)

class Pipeline(ObjectProxy, Generic[AnyStr]):  # type: ignore
    scripts: Set[Script[AnyStr]]
    @classmethod
    def proxy{{render_signature(inspect.signature(pipeline_kls.__init__), skip_defaults=True) | replace("self", "cls") | replace("-> None", "-> Pipeline[AnyStr]") }}: ...
    {% for name, signature in commands.items() -%}
    async def {{signature["name"]}}{{render_signature(signature["pipeline"], skip_defaults=True)}}: ...
    {% endfor %}
    async def watch{{render_signature(inspect.signature(pipeline_kls.watch), skip_defaults=True)}}: ...
    async def unwatch{{render_signature(inspect.signature(pipeline_kls.unwatch), skip_defaults=True)}}: ...
    def multi{{render_signature(inspect.signature(pipeline_kls.multi), skip_defaults=True)}}: ...
    async def execute{{render_signature(inspect.signature(pipeline_kls.execute), skip_defaults=True)}}: ...
    async def __aenter__(self) -> "Pipeline[AnyStr]":...
    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None: ...

class ClusterPipeline(ObjectProxy, Generic[AnyStr]):  # type: ignore
    @classmethod
    def proxy{{render_signature(inspect.signature(cluster_kls.__init__), skip_defaults=True) | replace("self", "cls") | replace("-> None", "-> ClusterPipeline[AnyStr]") }}: ...
    {% for name, signature in commands.items() -%}
    async def {{signature["name"]}}{{render_signature(signature["cluster"], skip_defaults=True)}}: ...
    {% endfor %}
    async def watch{{render_signature(inspect.signature(cluster_kls.watch), skip_defaults=True)}}: ...
    async def unwatch{{render_signature(inspect.signature(cluster_kls.unwatch), skip_defaults=True)}}: ...
    def multi{{render_signature(inspect.signature(cluster_kls.multi), skip_defaults=True)}}: ...
    async def execute{{render_signature(inspect.signature(cluster_kls.execute), skip_defaults=True)}}: ...
    async def __aenter__(self) -> "ClusterPipeline[AnyStr]":...
    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None: ...
"""
    env = Environment()
    env.globals.update(
        MIN_SUPPORTED_VERSION=MIN_SUPPORTED_VERSION,
        MAX_SUPPORTED_VERSION=MAX_SUPPORTED_VERSION,
        get_official_commands=get_official_commands,
        inspect=inspect,
        isinstance=isinstance,
        len=len,
        list=list,
        type=type,
        render_annotation=render_annotation,
        version_parse=version.parse,
        skip_command=skip_command,
        redis_command_link=redis_command_link,
        find_method=find_method,
        kls=kls,
        render_signature=render_signature,
        debug=True,
        pipeline_kls=coredis.pipeline.PipelineImpl,
        cluster_kls=coredis.pipeline.ClusterPipelineImpl,
    )
    stub_template = env.from_string(stub_template_str)
    response = stub_template.render(commands=commands)
    response = response.replace("coredis.pipeline.", "")
    response = response.replace("coredis.commands.constants.", "")
    response = response.replace("coredis.nodemanager.", "")
    with open(path, "w") as file:
        file.write(response)
    format_file_in_place(
        Path(path),
        fast=False,
        mode=FileMode(),
        write_back=WriteBack.YES,
    )


@click.option("--path", default="coredis/commands/_key_spec.py")
@code_gen.command()
def cluster_key_extraction(path):
    commands = get_commands()
    lookups = {}

    def _index_finder(command, search_spec, find_spec):
        if search_spec["type"] == "index":
            start_index = search_spec["spec"]["index"]
            command_offset = len(command.strip().split(" ")) - 1
            start_index = start_index - command_offset
            keystep = find_spec["spec"]["keystep"]
            if find_spec["type"] == "range":
                last_key = find_spec["spec"]["lastkey"]
                limit = find_spec["spec"]["limit"]
                if last_key == -1:
                    if limit > 0:
                        finder = f"args[{start_index}:len(args)-((len(args)-({start_index}+1))//{limit})]"
                    else:
                        finder = f"args[{start_index}:(len(args))"
                elif last_key == -2:
                    finder = f"args[{start_index}:(len(args) - 1)"
                else:
                    if start_index == start_index + last_key:
                        return f"(args[{start_index}],)"
                    finder = f"args[{start_index}:{start_index+last_key}"
                if keystep > 1:
                    finder += f":{keystep}]"
                else:
                    finder += "]"
                return finder
            elif find_spec["type"] == "keynum":
                first_key = find_spec["spec"]["firstkey"]
                keynumidx = find_spec["spec"]["keynumidx"]
                return f"args[{start_index+first_key}: {start_index+first_key}+int(args[{keynumidx+start_index}])]"
            else:
                raise RuntimeError(
                    f"Don't know how to handle {search_spec} with {find_spec}"
                )

    def _kw_finder(command, search_spec, find_spec):
        kw = search_spec["spec"]["keyword"]
        startfrom = search_spec["spec"]["startfrom"]
        command_offset = len(command.strip().split(" ")) - 1
        startfrom = startfrom - command_offset
        token = f'b"{kw}"'
        if startfrom >= 0:
            kw_expr = f"args.index({token}, {startfrom})"
        else:
            kw_expr = (
                f"len(args)-list(reversed(args)).index({token}, {abs(startfrom+1)})-1"
            )
        if find_spec["type"] == "range":
            last_key = find_spec["spec"]["lastkey"]
            limit = find_spec["spec"]["limit"]
            keystep = find_spec["spec"]["keystep"]
            finder = f"args[1+kwpos"
            if last_key == -1:
                if limit > 0:
                    lim = f":len(args)-(len(args)-(kwpos+1))//{limit}"
                else:
                    lim = ":len(args)"
                finder += f"{lim}"
            elif last_key == -2:
                finder += ":len(args)-1"
            elif last_key == 0:
                finder = f"(args[kwpos+1],)"
            else:
                raise RuntimeError("Unhandled last_key in keyword search")
            if keystep > 1:
                finder += f":{keystep}]"
            elif not last_key == 0:
                finder += "]"

            lamb = f"((lambda kwpos: tuple({finder}))({kw_expr}) if {token} in args else ())"
            return lamb
        else:
            raise RuntimeError(
                f"Don't know how to handle {search_spec} with {find_spec}"
            )

    for name, command in commands.items():
        key_specs = command.get("key_specs", [])
        for spec in key_specs:
            mode = (
                "RO"
                if spec.get("RO", False)
                else "OW"
                if spec.get("OW", False)
                else "RW"
                if spec.get("RW", False)
                else "RM"
            )
            begin_search = spec.get("begin_search", {})
            find_keys = spec.get("find_keys", {})
            if begin_search and find_keys:
                search_type = begin_search["type"]
                if search_type == "index":
                    lookups.setdefault(mode, {}).setdefault(name, []).append(
                        _index_finder(name, begin_search, find_keys)
                    )
                elif search_type == "keyword":
                    lookups.setdefault(mode, {}).setdefault(name, []).append(
                        _kw_finder(name, begin_search, find_keys)
                    )
                elif search_type == "unknown":
                    pass
                else:
                    raise RuntimeError(
                        f"Don't know how to handle {search_type} for {name} ({spec})"
                    )

    readonly = {}
    fixed_args = {"first": ["args[1],"], "second": ["args[2],"], "all": ["args[1:]"]}
    all = {"OBJECT": ["(args[2],)"], "DEBUG OBJECT": ["(args[1],)"]}

    for mode, commands in lookups.items():
        for command, exprs in commands.items():
            if mode == "RO":
                readonly[command] = exprs
            all.setdefault(command, []).extend(exprs)

    # KeyDB custom commands
    all["EXPIREMEMBER"] = all["EXPIRE"]
    all["EXPIREMEMBERAT"] = all["EXPIREAT"]
    all["PEXPIREMEMBERAT"] = all["PEXPIREAT"]
    all["KEYDB.HRENAME"] = ["(args[1],)"]
    all["KEYDB.MEXISTS"] = ["args[1:]"]
    all["OBJECT LASTMODIFIED"] = ["(args[1],)"]

    # RedisJSON
    all["JSON.DEBUG MEMORY"] = fixed_args["first"]
    all["JSON.DEL"] = fixed_args["first"]
    all["JSON.FORGET"] = fixed_args["first"]
    all["JSON.GET"] = fixed_args["first"]
    all["JSON.SET"] = fixed_args["first"]
    all["JSON.NUMINCRBY"] = fixed_args["first"]
    all["JSON.STRAPPEND"] = fixed_args["first"]
    all["JSON.STRLEN"] = fixed_args["first"]
    all["JSON.ARRAPPEND"] = fixed_args["first"]
    all["JSON.ARRINDEX"] = fixed_args["first"]
    all["JSON.ARRINSERT"] = fixed_args["first"]
    all["JSON.ARRLEN"] = fixed_args["first"]
    all["JSON.ARRPOP"] = fixed_args["first"]
    all["JSON.ARRTRIM"] = fixed_args["first"]
    all["JSON.OBJKEYS"] = fixed_args["first"]
    all["JSON.OBJLEN"] = fixed_args["first"]
    all["JSON.TYPE"] = fixed_args["first"]
    all["JSON.RESP"] = fixed_args["first"]
    all["JSON.TOGGLE"] = fixed_args["first"]
    all["JSON.CLEAR"] = fixed_args["first"]
    all["JSON.NUMMULTBY"] = fixed_args["first"]
    all["JSON.MERGE"] = fixed_args["first"]
    all["JSON.MSET"] = ["args[1::3]"]

    # bf
    all["BF.RESERVE"] = fixed_args["first"]
    all["BF.ADD"] = fixed_args["first"]
    all["BF.MADD"] = fixed_args["first"]
    all["BF.INSERT"] = fixed_args["first"]
    all["BF.EXISTS"] = fixed_args["first"]
    all["BF.MEXISTS"] = fixed_args["first"]
    all["BF.SCANDUMP"] = fixed_args["first"]
    all["BF.LOADCHUNK"] = fixed_args["first"]
    all["BF.INFO"] = fixed_args["first"]
    all["BF.CARD"] = fixed_args["first"]
    all["CF.RESERVE"] = fixed_args["first"]
    all["CF.ADD"] = fixed_args["first"]
    all["CF.ADDNX"] = fixed_args["first"]
    all["CF.INSERT"] = fixed_args["first"]
    all["CF.INSERTNX"] = fixed_args["first"]
    all["CF.EXISTS"] = fixed_args["first"]
    all["CF.MEXISTS"] = fixed_args["first"]
    all["CF.DEL"] = fixed_args["first"]
    all["CF.COUNT"] = fixed_args["first"]
    all["CF.SCANDUMP"] = fixed_args["first"]
    all["CF.LOADCHUNK"] = fixed_args["first"]
    all["CF.INFO"] = fixed_args["first"]
    all["CMS.INITBYDIM"] = fixed_args["first"]
    all["CMS.INITBYPROB"] = fixed_args["first"]
    all["CMS.INCRBY"] = fixed_args["first"]
    all["CMS.QUERY"] = fixed_args["first"]
    all["CMS.INFO"] = fixed_args["first"]
    all["CMS.MERGE"] = ["(args[1],) + args[3 : 3 + int(args[2])]"]
    all["TOPK.RESERVE"] = fixed_args["first"]
    all["TOPK.ADD"] = fixed_args["first"]
    all["TOPK.INCRBY"] = fixed_args["first"]
    all["TOPK.QUERY"] = fixed_args["first"]
    all["TOPK.LIST"] = fixed_args["first"]
    all["TOPK.INFO"] = fixed_args["first"]
    all["TOPK.COUNT"] = fixed_args["first"]
    all["TDIGEST.CREATE"] = fixed_args["first"]
    all["TDIGEST.RESET"] = fixed_args["first"]
    all["TDIGEST.ADD"] = fixed_args["first"]
    all["TDIGEST.MERGE"] = ["(args[1],) + args[3 : 3 + int(args[2])]"]
    all["TDIGEST.MIN"] = fixed_args["first"]
    all["TDIGEST.MAX"] = fixed_args["first"]
    all["TDIGEST.QUANTILE"] = fixed_args["first"]
    all["TDIGEST.CDF"] = fixed_args["first"]
    all["TDIGEST.TRIMMED_MEAN"] = fixed_args["first"]
    all["TDIGEST.RANK"] = fixed_args["first"]
    all["TDIGEST.REVRANK"] = fixed_args["first"]
    all["TDIGEST.BYRANK"] = fixed_args["first"]
    all["TDIGEST.BYREVRANK"] = fixed_args["first"]
    all["TDIGEST.INFO"] = fixed_args["first"]

    # timeseries
    all["TS.CREATE"] = fixed_args["first"]
    all["TS.CREATERULE"] = ["args[1:3]"]
    all["TS.ALTER"] = fixed_args["first"]
    all["TS.ADD"] = fixed_args["first"]
    all["TS.MADD"] = ["args[1:-1:3]"]
    all["TS.INCRBY"] = fixed_args["first"]
    all["TS.DECRBY"] = fixed_args["first"]
    all["TS.DELETERULE"] = ["args[1:3]"]
    all["TS.GET"] = fixed_args["first"]
    all["TS.INFO"] = fixed_args["first"]
    all["TS.REVRANGE"] = fixed_args["first"]
    all["TS.RANGE"] = fixed_args["first"]
    all["TS.DEL"] = fixed_args["first"]

    # Search
    all["FT.CREATE"] = fixed_args["first"]
    all["FT.INFO"] = fixed_args["first"]
    all["FT.ALTER"] = fixed_args["first"]
    all["FT.ALIASADD"] = fixed_args["first"]
    all["FT.ALIASUPDATE"] = fixed_args["first"]
    all["FT.ALIASDEL"] = fixed_args["first"]
    all["FT.TAGVALS"] = fixed_args["first"]
    all["FT.CONFIG GET"] = fixed_args["first"]
    all["FT.CONFIG SET"] = fixed_args["first"]
    all["FT.SEARCH"] = fixed_args["first"]
    all["FT.AGGREGATE"] = fixed_args["first"]
    all["FT.CURSOR GET"] = fixed_args["first"]
    all["FT.CURSOR DEL"] = fixed_args["first"]
    all["FT.SYNUPDATE"] = fixed_args["first"]
    all["FT.SYNDUMP"] = fixed_args["first"]
    all["FT.SPELLCHECK"] = fixed_args["first"]
    all["FT.DICTADD"] = fixed_args["first"]
    all["FT.DICTDEL"] = fixed_args["first"]
    all["FT.DICTDUMP"] = fixed_args["first"]
    all["FT.DROPINDEX"] = fixed_args["first"]

    # Autocomplete
    all["FT.SUGADD"] = fixed_args["first"]
    all["FT.SUGDEL"] = fixed_args["first"]
    all["FT.SUGGET"] = fixed_args["first"]
    all["FT.SUGLEN"] = fixed_args["first"]

    # RedisGraph
    all["GRAPH.QUERY"] = fixed_args["first"]
    all["GRAPH.DELETE"] = fixed_args["first"]
    all["GRAPH.EXPLAIN"] = fixed_args["first"]
    all["GRAPH.PROFILE"] = fixed_args["first"]
    all["GRAPH.SLOWLOG"] = fixed_args["first"]
    all["GRAPH.CONSTRAINT CREATE"] = fixed_args["first"]
    all["GRAPH.CONSTRAINT DROP"] = fixed_args["first"]
    all["GRAPH.RO_QUERY"] = fixed_args["first"]

    key_spec_template = """
from __future__ import annotations

from coredis._utils import b
from coredis.typing import Callable, ClassVar, Dict, Tuple, ValueT

class KeySpec:
    READONLY: ClassVar[Dict[bytes, Callable[[Tuple[ValueT, ...]], Tuple[ValueT, ...]]]] = {{ '{' }}
    {% for command, exprs in readonly.items() %}
        b"{{command}}": lambda args: ({{exprs | join("+")}}),
    {% endfor %}
    {{ '}' }}
    ALL: ClassVar[Dict[bytes, Callable[[Tuple[ValueT, ...]], Tuple[ValueT, ...]]]] = {{ '{' }}
    {% for command, exprs in all.items() %}
        b"{{command}}": lambda args: ({{exprs | join("+")}}) ,
    {% endfor %}
    {{ '}' }}

    @classmethod
    def extract_keys(cls, *arguments: ValueT, readonly_command: bool = False) -> Tuple[ValueT, ...]:
        if len(arguments) <= 1:
            return ()

        command=b(arguments[0])

        try:
            if readonly_command and command in cls.READONLY:
                return cls.READONLY[command](arguments)
            else:
                return cls.ALL[command](arguments)
        except KeyError:
            return  ()
    """
    env = Environment()
    env.globals.update(
        command_enum=command_enum,
        sanitized=sanitized,
    )
    tmpl = env.from_string(key_spec_template)
    response = tmpl.render(all=all, readonly=readonly)
    with open(path, "w") as file:
        file.write(response)
    format_file_in_place(
        Path(path),
        fast=False,
        mode=FileMode(),
        write_back=WriteBack.YES,
    )


if __name__ == "__main__":
    code_gen()
