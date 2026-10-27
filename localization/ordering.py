"""迭代Kosaraju压缩循环；同层独立单元一起返回。"""


def layers(nodes, dependencies):
    """dependencies中的(A, B)表示A需要B，故B应先处理。"""
    graph = {n: set() for n in nodes}
    reverse = {n: set() for n in graph}
    for source, target in dependencies:
        if source not in graph or target not in graph:
            raise ValueError("依赖边引用不存在的单元")
        if source != target:
            graph[source].add(target)
            reverse[target].add(source)
    visited, finish = set(), []
    for start in sorted(graph):
        stack = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                finish.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend((v, False) for v in sorted(graph[node], reverse=True) if v not in visited)
    belongs, components = {}, []
    for start in reversed(finish):
        if start in belongs:
            continue
        group, stack = [], [start]
        number = len(components)
        while stack:
            node = stack.pop()
            if node in belongs:
                continue
            belongs[node] = number
            group.append(node)
            stack.extend(reverse[node])
        components.append(group)
    remaining = {i: {belongs[v] for n in group for v in graph[n] if belongs[v] != i}
                 for i, group in enumerate(components)}
    result = []
    while remaining:
        ready = {n for n, deps in remaining.items() if not deps}
        if not ready:
            raise ValueError("循环压缩后仍不能排序")
        result.append(sorted(n for c in ready for n in components[c]))
        remaining = {n: deps - ready for n, deps in remaining.items() if n not in ready}
    return result
