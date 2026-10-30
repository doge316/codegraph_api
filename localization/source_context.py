"""将已核验的源码提取中间结果整理为翻译模型可读的原文上下文。"""
import re


def render_source(result):
    """只负责组织原文，不重新定位入口、推断依赖或生成业务代码。"""
    warnings = []
    for resolution in result['resolutions']:
        if resolution['status'] == 'not_found':
            warnings.append('未找到入口：' + resolution['name'])
        elif resolution['status'] == 'ambiguous':
            candidates = '；'.join(
                f"{c['qualified_name']}（{c['file']}:{c['start_line']}，{c.get('signature') or '签名未知'}）"
                for c in resolution['candidates'])
            warnings.append(f"入口存在歧义：{resolution['name']}；候选：{candidates}")
    for diagnostic in result['diagnostics']:
        location = diagnostic.get('location')
        where = f"（{location['file_path']}:{location['start_line']}）" if location else ''
        warnings.append(f"[{diagnostic['code']}] {diagnostic['message']}{where}")
    for dependency in result['external_dependencies']:
        warnings.append('外部依赖（未附实现）：' + dependency['name'])
    for resource in result['resources']:
        state = {True: '存在，未附内容', False: '不存在', None: '路径未确认'}[resource['exists']]
        warnings.append(f"资源引用：{resource['path'] or resource['expression']}（{state}）")

    content = ''
    if result['status'] in {'ready', 'partial'}:
        units = {u['key']: u for u in result['units']}
        ordered = [key for batch in result['batches'] for key in batch]
        if not units or len(ordered) != len(units) or set(ordered) != set(units):
            raise ValueError('源码单元与依赖优先批次不一致')
        tag = {'Python': 'python', 'Java': 'java', 'C#': 'csharp', 'C++': 'cpp'}[result['source_language']]
        lines = ['源语言：' + result['source_language'],
                 '请求入口：' + '、'.join(r['name'] for r in result['resolutions'])]
        emitted = set()
        for index, batch in enumerate(result['batches'], 1):
            lines.append(f'\n## 依赖层 {index}')
            for key in batch:
                unit = units[key]
                lines.append('\n### ' + unit['qualified_name'])
                for context in unit['import_fragments']:
                    _append_fragment(lines, context, tag, emitted, context=True)
                for fragment in unit['fragments']:
                    _append_fragment(lines, fragment, tag, emitted)
        content = '\n'.join(lines) + '\n'
    return {'status': result['status'], 'content': content,
            'warnings': list(dict.fromkeys(warnings))}


def _append_fragment(lines, fragment, tag, emitted, context=False):
    """源码单元已按字节区间去重；展示时只合并重复导入，不合并同文定义。"""
    source = fragment['source']
    identity = (fragment['file'], fragment.get('start_line'), fragment.get('end_line'), source)
    if context and identity in emitted:
        return
    if context:
        emitted.add(identity)
    label = '文件：' + fragment['file']
    if fragment.get('start_line') is not None:
        label += f"，第 {fragment['start_line']}—{fragment['end_line']} 行"
    if context:
        label += '（导入上下文）'
    # 原文可能包含Markdown围栏；加长外层围栏，避免代码块提前结束。
    fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', source)), default=0))
    block = fence + tag + '\n' + source
    if not source.endswith('\n'):
        block += '\n'
    lines.extend(['\n' + label, block + fence])
