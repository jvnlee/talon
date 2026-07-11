from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    pass


@dataclass(frozen=True)
class Column(Node):
    name: str


@dataclass(frozen=True)
class Const(Node):
    value: float


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Compare(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Func(Node):
    name: str
    args: tuple[Node, ...]
