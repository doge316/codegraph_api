"""总配置与数据集位置；不读取YAML，不猜测当前翻译对。"""
from importlib import import_module
from pathlib import Path

ALIASES = {"python": "Python", "java": "Java", "c#": "C#", "csharp": "C#",
           "c++": "C++", "cpp": "C++", "rust": "Rust"}
SDK_LANGUAGES = {"Python": "python", "Java": "java", "C#": "csharp", "C++": "cpp", "Rust": "rust"}
PAIRS = {"C#_to_Java", "C++_to_Python", "Java_to_C#", "Java_to_Python",
         "Python_to_C++", "Python_to_Java", "Python_to_Rust"}
SKIP = {".git", ".codegraph", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "target", "bin", "obj"}


def language(value):
    if not isinstance(value, str) or value.lower() not in ALIASES:
        raise ValueError("未知语言：" + str(value))
    return ALIASES[value.lower()]


def pair_name(value):
    if not isinstance(value, str) or value.lower().count("_to_") != 1:
        raise ValueError("翻译对格式应为Java_to_Python等")
    source, target = value.lower().split("_to_")
    pair = language(source) + "_to_" + language(target)
    if pair not in PAIRS:
        raise ValueError("不支持的翻译对：" + pair)
    return pair


def route(value, purpose):
    sdk = SDK_LANGUAGES[language(value)]
    if purpose == "Source" and sdk == "rust":
        raise ValueError("Rust不是当前七翻译对的源语言")
    module = import_module(".languages." + sdk, "localization")
    name = {"python": "Python", "java": "Java", "csharp": "CSharp", "cpp": "Cpp", "rust": "Rust"}[sdk]
    return getattr(module, purpose + name + "Route")()


def repository(root, pair, name):
    if not isinstance(name, str) or not name.strip() or name in {".", ".."} or any(c in name for c in "/\\:\0"):
        raise ValueError("仓库名必须是单个目录名")
    base = (root / pair).resolve()
    result = (base / name).resolve()
    if result.parent != base or not result.is_relative_to(root) or not result.is_dir():
        raise ValueError("仓库不存在或路径越界：" + str(result))
    return result


def public_directories(target):
    """保留Java包目录和Rust tests/public_tests，不进入其他测试文件。"""
    import os
    roots = []
    for current, dirs, _ in os.walk(target, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (here / d).is_symlink()
                         and not getattr(here / d, "is_junction", lambda: False)())
        if here.name in {"public_test", "public_tests"}:
            roots.append(here)
            dirs[:] = []
    if not roots:
        raise ValueError("未找到public_test或public_tests目录：" + str(target))
    return roots


def files_under(root):
    import os
    paths = []
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (here / d).is_symlink()
                         and not getattr(here / d, "is_junction", lambda: False)())
        for name in sorted(names):
            path = here / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("输入文件链接不受支持：" + str(path))
            if path.suffix in {".pyc", ".pyo", ".log"}:
                continue
            paths.append(path)
    return paths
