"""实体差异守卫测试（entity_guard.py，纯 stdlib difflib/re）。

行为矩阵 9 例 + 防御 2 例：True=差异片段含实体应 miss；False=纯语言改写可命中。
覆盖：化学式/年份/数值/中文数字/型号 五类实体拦截，语言改写/虚词/同义动词/标点/空串 放行。
"""

from phxsc.cache.entity_guard import entity_diff_guard


class TestEntityDiffGuard:
    def test_chemical_formula_diff(self):
        assert entity_diff_guard("Mn3Ga 反铁磁", "Mn3Sn 反铁磁") is True

    def test_year_diff(self):
        assert entity_diff_guard("2024 钙钛矿进展", "2025 钙钛矿进展") is True

    def test_numeric_param_diff(self):
        assert entity_diff_guard("带隙 1.5eV 材料", "带隙 2.0eV 材料") is True

    def test_chinese_numeral_diff(self):
        assert entity_diff_guard("三价锰氧化物", "四价锰氧化物") is True

    def test_model_name_diff(self):
        assert entity_diff_guard("GPT-4 总结", "GPT-5 总结") is True

    def test_language_rewrite_allowed(self):
        assert entity_diff_guard("钙钛矿稳定性综述", "perovskite 稳定性领域进展") is False

    def test_function_word_allowed(self):
        assert entity_diff_guard("帮我总结一下", "请总结一下") is False

    def test_synonym_verb_allowed(self):
        assert entity_diff_guard("总结一下", "综述一下") is False

    def test_identical_strings_allowed(self):
        assert entity_diff_guard("完全相同字符串", "完全相同字符串") is False

    def test_punctuation_only_allowed(self):
        assert entity_diff_guard("钙钛矿稳定性，", "钙钛矿稳定性。") is False

    def test_empty_vs_nonempty_no_crash(self):
        assert entity_diff_guard("", "非空") is False

    def test_cjk_year_boundary_blocked(self):
        assert entity_diff_guard("2024年钙钛矿稳定性综述", "2025年钙钛矿稳定性综述") is True

    def test_single_letter_identifier_material_blocked(self):
        assert entity_diff_guard("材料A的合成方法是什么", "材料B的合成方法是什么") is True

    def test_single_letter_identifier_catalyst_blocked(self):
        assert entity_diff_guard("催化剂A对反应的影响", "催化剂B对反应的影响") is True

    def test_cjk_multi_digit_boundary_blocked(self):
        assert entity_diff_guard("第42章的内容", "第43章的内容") is True

    def test_digit_letter_combination_blocked(self):
        assert entity_diff_guard("图3a的能带结构", "图3b的能带结构") is True

    def test_chinese_synonym_rewrite_allowed(self):
        assert entity_diff_guard("研究现状", "研究进展") is False

    def test_function_word_removal_allowed(self):
        assert entity_diff_guard("钙钛矿热降解的机理", "钙钛矿热降解机理") is False

    def test_cross_language_synonym_allowed(self):
        assert entity_diff_guard("perovskite 稳定性", "钙钛矿稳定性") is False
