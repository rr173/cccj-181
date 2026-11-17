"""Relation reference graph: cycle detection and dependency ordering.

Everything here is iterative — cycle handling must never depend on
recursion depth, and deep nesting must not hit the interpreter's
recursion limit either.
"""
from __future__ import annotations

from collections import deque


def build_graph(relations: dict) -> dict:
    """rid -> sorted list of existing child relation ids."""
    graph = {}
    for rid, members in relations.items():
        children = {
            m["ref"]
            for m in members
            if m["type"] == "relation" and m["ref"] in relations
        }
        graph[rid] = sorted(children)
    return graph


def _witness_cycle(graph: dict, scc: list) -> list:
    """One concrete cycle chain inside an SCC, e.g. [400, 401, 400]."""
    members = set(scc)
    start = min(scc)
    if start in graph.get(start, ()):  # self loop
        return [start, start]
    # BFS from start's successors back to start, staying inside the SCC
    prev = {}
    queue = deque()
    for nxt in graph.get(start, ()):
        if nxt in members and nxt != start and nxt not in prev:
            prev[nxt] = start
            queue.append(nxt)
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, ()):
            if nxt == start:
                chain = [node]
                while chain[-1] != start:
                    chain.append(prev[chain[-1]])
                chain.reverse()
                chain.append(start)
                return chain
            if nxt in members and nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    return [start]  # unreachable for a genuine SCC


def find_cycles(graph: dict) -> dict:
    """Iterative Tarjan SCC.  Returns {rid: witness_cycle_chain} for every
    relation that sits on a reference cycle (SCC of size > 1 or self-loop)."""
    index_of = {}
    low = {}
    on_stack = {}
    stack = []
    counter = [0]
    cyclic = {}

    for root in sorted(graph):
        if root in index_of:
            continue
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        work = [(root, iter(graph[root]))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(graph.get(nxt, ()))))
                    descended = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == node:
                        break
                if len(scc) > 1 or scc[0] in graph.get(scc[0], ()):
                    chain = _witness_cycle(graph, scc)
                    for rid in scc:
                        cyclic[rid] = chain
    return cyclic


def postorder(relations: dict, skip: set) -> list:
    """Children-before-parents ordering over the acyclic remainder.

    ``skip`` contains cyclic relations (already resolved).  Missing
    references are gaps, not edges, so they are not traversed.
    """
    order = []
    state = {}  # rid -> 1 (visiting) / 2 (done)
    for root in sorted(relations):
        if root in skip or state.get(root) == 2:
            continue
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                state[node] = 2
                order.append(node)
                continue
            if state.get(node):
                continue
            state[node] = 1
            stack.append((node, True))
            for m in relations[node]:
                if m["type"] == "relation":
                    child = m["ref"]
                    if child in relations and child not in skip and not state.get(child):
                        stack.append((child, False))
    return order
