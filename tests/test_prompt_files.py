"""提示词文件加载器的回归（P15）。

这一层守的是**「提示词住在哪、有哪几份、怎么拼、缺了会怎样」**，不是提示词写得好不好。
五条最重要的断言：

1. **恰好五份**。2026-09-30 把「找下一题控件 / 起始标定 / 收尾确认」三套提示词
   删掉了（视觉组只回答**一份**固定格式的观测），所以文件集合从 7 份变 5 份。
   钉死集合而不是「至少有几份」：多出一份没人拼装的提示词，是最容易悄悄死掉的
   那种文件 —— 它不再影响任何行为，却让人以为改它有用。
2. **共享契约必须同时出现在两份拼装结果里** —— 它叫「共享」，
   少给一边就会出现「两个模型对同一件事说法不同」的裂缝，而这种裂缝
   在运行时不报错，只是行为慢慢漂移。
3. **拼装顺序固定**：共享契约在前、专职规则在后。后写的更具体，压得住前者；
   反过来会让通用规则覆盖掉专职规则（例如「无法确定时给最可能的一项」
   会盖掉「题面缺失就空作答」）。
4. **``READ_SYSTEM_PROMPT`` 就是「00 + 10 + 注册技能目录」**。读图只有这一份系统提示词，
   它是视觉组行为的唯一定义点 —— 拼错文件、漏拼一段，效果与改错提示词完全一样，
   而现场看不出任何异常。
5. **缺文件必须大声失败**。空提示词不会让任何东西报错，它只是让模型自由发挥，
   而现场看起来完全正常 —— 这是最贵的一种失败。

全部是纯内存用例，不连模型、不起浏览器，所以永不 skip。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from solve import prompt_files
from solve.prompt_files import (
    LIBRARY_PROMPT_FILE,
    SHARED_PROMPT_FILE,
    SOLVER_PROMPT_FILE,
    TRAINING_PROMPT_FILE,
    VISION_PROMPT_FILE,
    PromptFileMissingError,
    load_prompt,
    prompts_dir,
    solver_system_prompt,
    training_system_prompt,
    vision_system_prompt,
)
from solve.reader import READ_SYSTEM_PROMPT

#: ``prompts/`` 里**恰好**该有的五份文件。
#:
#: 顺序按「谁先被读」排：共享契约 → 视觉组 → 解题组 → 方式库 → 训练总结。
#: 这里写死名字是有意的（而不是从目录里扫出来再断言「非空」）：
#: 少了任何一份都会在**导入期**让 ``solve.reader`` / ``solve.prompt_files`` 炸掉，
#: 而多出的那一份会成为一个「改了没反应」的陷阱。
PROMPT_FILES: tuple[str, ...] = (
    SHARED_PROMPT_FILE,
    VISION_PROMPT_FILE,
    SOLVER_PROMPT_FILE,
    LIBRARY_PROMPT_FILE,
    TRAINING_PROMPT_FILE,
)

#: 共享契约里必须同时出现在两边、且逐字一致的几条约定。
SHARED_MUST_APPEAR = (
    "共享契约",  # 分节标题：证明共享段确实拼进去了
    "$...$",  # LaTeX 口径
    "大小写",  # 大小写纪律
    "bmatrix",  # 禁止用矩阵环境凑区间
    "qid",  # 题目指纹
    "clipped",  # 质量字段
)


# --------------------------------------------------------------------------- #
# 目录与读取
# --------------------------------------------------------------------------- #
def test_prompts_dir_is_the_repo_one() -> None:
    """源码态必须解析到仓库根下的 ``prompts/``（不是 cwd、不是 ``_internal``）。"""
    found = prompts_dir()
    assert found.is_dir()
    assert found.name == "prompts"
    for name in PROMPT_FILES:
        assert (found / name).is_file(), f"缺提示词文件 {name}"


def test_prompt_set_is_exactly_the_five_in_use() -> None:
    """**恰好五份**，一份不多一份不少。

    判据是「目录里的 ``*.md`` 集合 == 代码里认的那五个常量」：
    多出来的文件说明有人加了一份**不会被任何地方拼装**的提示词
    （改它不会有任何效果，却看起来像是生效了）；
    少了文件则由上一条与导入期共同拦住。
    """
    found = sorted(path.name for path in prompts_dir().glob("*.md"))
    assert found == sorted(PROMPT_FILES), (
        f"prompts/ 里的 Markdown 与实际使用的 {len(PROMPT_FILES)} 份不一致：{found}"
    )


def test_every_prompt_file_is_non_trivial() -> None:
    """每份文件都得有像样的内容 —— 防止有人把它清空而没人发现。"""
    for name in PROMPT_FILES:
        text = load_prompt(name)
        assert len(text) > 500, f"{name} 只有 {len(text)} 字符，像是被清空了"
        assert text.lstrip().startswith("#"), f"{name} 应当以一级标题开头"


# --------------------------------------------------------------------------- #
# 拼装
# --------------------------------------------------------------------------- #
def test_shared_contract_is_glued_into_both_prompts() -> None:
    """**本文件最重要的一条**：共享契约两边都得到。

    没有它，视觉组与解题组就会各自理解 LaTeX 口径、标号语义和「不知道怎么办」——
    两套理解都不会报错，只会让跨模型的缝隙慢慢变大。
    """
    vision = vision_system_prompt()
    solver = solver_system_prompt()

    for marker in SHARED_MUST_APPEAR:
        assert marker in vision, f"视觉组 prompt 缺共享约定：{marker}"
        assert marker in solver, f"解题组 prompt 缺共享约定：{marker}"


def test_read_system_prompt_is_shared_vision_and_skill_catalog() -> None:
    """读图提示词由共享契约、视觉规则、技能注册表目录组成。

    system prompt 只在 ``vision_system_prompt()`` 拼装，导入时读一次。技能选择
    清单必须来自注册表，既不遗漏也不允许提示词与代码的技能映射漂移。
    """
    shared = load_prompt(SHARED_PROMPT_FILE)
    vision = load_prompt(VISION_PROMPT_FILE)

    assert vision_system_prompt() == READ_SYSTEM_PROMPT, (
        "READ_SYSTEM_PROMPT 必须由 vision_system_prompt() 给出（读图只有这一份）"
    )
    assert f"{shared}{prompt_files._SEPARATOR}{vision}" in READ_SYSTEM_PROMPT
    assert READ_SYSTEM_PROMPT.endswith(prompt_files._SEPARATOR + prompt_files.vision_skill_catalog())
    assert "最多 3 道" in vision
    assert "formulas" not in vision
    assert "公式坐标索引" in vision
    assert "本地注册表自动选择" in prompt_files.vision_skill_catalog()
    assert "skill_id" in vision


def test_training_prompt_is_shared_plus_training_rules() -> None:
    """训练总结同样「共享契约 + 自己的专职规则」，且**不与读题共用**。

    它问的是「这次运行有什么可复用的经验」，与「这一屏里有什么题」是两个输出契约；
    共用一个 system prompt 会让模型按读题的形状回答训练问题。
    """
    shared = load_prompt(SHARED_PROMPT_FILE)
    training = load_prompt(TRAINING_PROMPT_FILE)
    composed = training_system_prompt()

    assert composed == f"{shared}{prompt_files._SEPARATOR}{training}"
    assert composed != READ_SYSTEM_PROMPT


def test_shared_contract_comes_first() -> None:
    """顺序固定：共享契约在前、专职规则在后。

    专职规则更具体，必须在后面 —— 反过来的话，共享段里较宽松的兜底
    （例如「无法确定时可以给最可能的一项」）会压掉解题组的「空作答」纪律。
    """
    vision = vision_system_prompt()
    solver = solver_system_prompt()

    assert vision.index("共享契约") < vision.index("视觉组 · 专职规则")
    assert solver.index("共享契约") < solver.index("解题组 · 专职规则")


def test_each_side_gets_its_own_rules_and_not_the_others() -> None:
    """两份 prompt 不能互相串味。

    注意「串味」的判据是**对方那份「专职规则」有没有被拼进来**，
    而不是「有没有提到对方的概念」—— 共享契约里本来就要写清两边的接口与输出格式，
    那是它该干的事（例如 §1 的分工表里就会写解题组输出 ``chosen_labels``）。
    """
    vision = vision_system_prompt()
    solver = solver_system_prompt()

    # 视觉组：抄录契约在，且**明确不作答**
    assert "抄录" in vision
    assert "不给答案" in vision and "不做任何计算" in vision
    # 画面边界是视觉组独有的出口
    assert "clipped" in vision and "more_below" in vision

    # 解题组：输出契约在，且**空作答是正确行为**
    assert "chosen_labels" in solver
    assert "空作答" in solver

    # 对方的「专职规则」段落绝不能被拼进来
    assert "解题组 · 专职规则" not in vision
    assert "视觉组 · 专职规则" not in solver


def test_solver_prompt_forbids_best_guess_on_broken_stem() -> None:
    """题面不完整时**不给"最可能的一项"兜底** —— 猜错时看不出来。

    这条与旧实现（``无法确定时仍给最可能的一项``）是**刻意相反**的：v0.2.0 的题面
    由视觉模型抄录而来，本身可能就是错的，而错误不可见 —— 猜一次等于把读题错误
    洗成一次正常作答。
    """
    solver = solver_system_prompt()
    assert "不要作答" in solver
    assert "停下来等人" in solver
    # 必须把「不给兜底」的理由写出来，否则模型会按常识去补一个答案
    assert "看不出来" in solver


def test_vision_prompt_requires_latex_and_case() -> None:
    """视觉组的四条特化必须都在：行内 LaTeX、禁止矩阵凑区间、大小写、$?$ 占位。"""
    vision = vision_system_prompt()
    assert "行内 LaTeX" in vision
    assert "绝对禁止" in vision and "bmatrix" in vision
    assert "严格区分大小写" in vision
    assert "$?$" in vision


# --------------------------------------------------------------------------- #
# 缺文件 / 坏文件：必须大声失败
# --------------------------------------------------------------------------- #
def test_missing_prompt_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件缺失 → ``PromptFileMissingError``，**绝不给默认提示词兜底**。"""
    monkeypatch.setattr(prompt_files, "prompts_dir", lambda: tmp_path)
    with pytest.raises(PromptFileMissingError):
        load_prompt(SHARED_PROMPT_FILE)


def test_empty_prompt_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件在但是空的 → 同样大声失败（这是最难发现的那种"缺"）。"""
    (tmp_path / SHARED_PROMPT_FILE).write_text("\n\n   \n", encoding="utf-8")
    monkeypatch.setattr(prompt_files, "prompts_dir", lambda: tmp_path)
    with pytest.raises(PromptFileMissingError):
        load_prompt(SHARED_PROMPT_FILE)


def test_non_utf8_prompt_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 UTF-8 → 报错，**不许用 errors="replace" 吞掉**。

    提示词里出现替换字符，模型就照着一堆 ``?`` 执行 —— 那比直接报错难查得多。
    """
    (tmp_path / SHARED_PROMPT_FILE).write_bytes(b"\xff\xfe\x00# \xc3\x28broken")
    monkeypatch.setattr(prompt_files, "prompts_dir", lambda: tmp_path)
    with pytest.raises(PromptFileMissingError):
        load_prompt(SHARED_PROMPT_FILE)


def test_prompts_dir_reports_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """找不到目录时，报错要把**试过哪些路径**列出来 —— 打包后最常见的就是这个。

    （源码态能跑、打包后 ``_MEIPASS`` 里没有 ``prompts/``，只报一句
    「找不到」会让人从「提示词写错了」开始查，方向全错。）
    """
    monkeypatch.setattr(prompt_files, "_candidates", lambda: [tmp_path / "nowhere"])
    with pytest.raises(PromptFileMissingError) as excinfo:
        prompt_files.prompts_dir()
    assert "nowhere" in str(excinfo.value)
    assert "prompts" in str(excinfo.value)
