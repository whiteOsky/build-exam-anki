# 参与贡献

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

## 修改原则

- 保持 `SKILL.md` 简洁，详细规则放入 `references/`。
- 不改变八字段、覆盖状态和稳定 GUID 等公共契约，除非同时提供迁移方案。
- 所有用户可见命令帮助、错误和报告使用中文。
- 新增行为必须补测试；缺陷修复先增加可复现测试。
- 不提交教材、题库、个人卡片、绝对路径、APKG 成品或打印缓存。

## 提交前检查

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/*.py tools/*.py tests/*.py
python3 tools/check_public.py
python3 tools/package_release.py --force
```

Pull Request 应说明行为变化、验证命令和剩余边界。涉及输出格式时，附最小脱敏示例，不附真实教材内容。
