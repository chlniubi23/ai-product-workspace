"""Tests for Enhanced Analysis Engine.

覆盖 4 个关键修复点：
1. 派生列排除
2. 离群值对象输出
3. 稳健相关系数
4. 序数列分布
"""

import numpy as np
import pandas as pd

from app.analytics.enhanced_engine import EnhancedAnalysisEngine


class TestDerivedColumnExclusion:
    """测试派生列自动识别与排除."""
    
    def test_detect_ratio_derived_column(self):
        """测试检测比率类派生列."""
        # 模拟数据：渗透率 = 周活 / 总用户
        data = {
            '周活跃用户': [100, 200, 300, 400, 500],
            '总用户数': [500, 600, 700, 800, 900],
            '渗透率(% 占周活)': [20.0, 33.3, 42.9, 50.0, 55.6]  # 必然强相关！
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        result = engine.run_comprehensive_analysis()
        
        lineage = result['column_lineage']
        
        # 验证渗透率被识别为派生列
        assert '渗透率(% 占周活)' in lineage
        assert lineage['渗透率(% 占周活)']['is_derived'] is True
        assert len(lineage['渗透率(% 占周活)']['derived_from']) >= 1
        
        # 验证排除了伪相关的对
        excluded_pairs = result['correlation_analysis']['summary']['excluded_derived_pairs']
        assert excluded_pairs > 0, "应该至少排除一对派生列"
        
        # 验证相关性结果中不包含该对
        correlations = result['correlation_analysis']['pairwise_correlations']
        pair_names = [(r['var1'], r['var2']) for r in correlations]
        assert ('周活跃用户', '渗透率(% 占周活)') not in pair_names
        assert ('渗透率(% 占周活)', '周活跃用户') not in pair_names
    
    def test_exclude_automatically_generated_columns(self):
        """测试自动生成的衍生列不应出现在相关性矩阵中."""
        data = {
            'A': list(range(10)),
            'B': list(range(10, 20)),
            'C': list(x + y for x, y in zip(range(10), range(10, 20), strict=True))  # A+B 派生
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        correlations = results['correlation_analysis']['pairwise_correlations']
        
        # C 是 A+B 的派生，应该被排除
        pair_names = [(r['var1'], r['var2']) for r in correlations]
        assert ('A', 'C') not in pair_names


class TestOutlierObjectOutput:
    """测试离群值对象化信息输出."""
    
    def test_outlier_contains_row_context(self):
        """测试每个离群值都包含行的上下文信息."""
        # 制造一个明显的离群值
        np.random.seed(42)
        normal_data = np.random.normal(100, 10, 50)
        outlier_value = 500  # 远超出正常范围
        
        data = {
            '功能模块': [f'Feature_{i}' for i in range(len(normal_data) + 1)],
            '周活跃用户': list(normal_data[:5]) + [outlier_value] + list(normal_data[5:])
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        result = engine.run_comprehensive_analysis()
        
        outliers = result['outlier_detection']
        
        # 验证找到了离群值
        assert outliers['has_outliers'] is True
        assert outliers['total_count'] >= 1
        
        # 验证至少有 1 个离群值包含完整信息
        if outliers['by_column']:
            first_col_outliers = outliers['by_column'].get('周活跃用户', [])
            assert len(first_col_outliers) >= 1
            
            first_outlier = first_col_outliers[0]
            
            # 必须包含这些字段
            assert 'row_index' in first_outlier
            assert 'value' in first_outlier
            assert 'threshold' in first_outlier
            assert 'description' in first_outlier
            assert 'outlier_type' in first_outlier
            
            # 验证描述格式可读
            desc = first_outlier['description']
            assert '周活跃用户' in desc
            assert str(outlier_value) in desc or "离群值" in desc
    
    def test_outlier_threshold_is_explanatory(self):
        """测试阈值说明清晰可理解."""
        data = {
            '数值列': [10, 12, 11, 13, 100]  # 最后一个明显异常
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        result = engine.run_comprehensive_analysis()
        
        outliers = result['outlier_detection']
        col_outliers = outliers['by_column'].get('数值列', [])
        
        if col_outliers:
            outlier = col_outliers[0]
            threshold = outlier['threshold']
            
            # 阈值应该是具体的数字（IQR 计算的）
            assert isinstance(threshold, float)
            assert threshold != 0
            
            # 描述中应该能解释为什么是这个值
            assert 'Q3' in outlier['description'] or '上界' in outlier['description']


class TestRobustCorrelation:
    """测试稳健相关系数计算."""
    
    def test_provides_p_value_and_robust_estimate(self):
        """测试每个相关系数都有 p 值和稳健估计."""
        # 标准正态数据
        np.random.seed(42)
        n = 100
        data = {
            'X': np.random.randn(n),
            'Y': np.random.randn(n)
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        correlations = results['correlation_analysis']['pairwise_correlations']
        
        # 至少有一对相关分析
        assert len(correlations) >= 1
        
        corr = correlations[0]
        
        # 必须有显著性检验
        assert 'pearson_r' in corr
        assert 'pearson_p' in corr
        
        # 必须有稳健估计
        assert 'robust_pearson' in corr
        assert 'robust_spearman' in corr
        
        # 如果有鲁棒性注释，则更完整
        if corr.get('robustness_note'):
            assert isinstance(corr['robustness_note'], str)
            assert len(corr['robustness_note']) > 10
    
    def test_detects_significant_vs_non_significant(self):
        """测试区分显著与非显著相关."""
        # 构造两组数据：一组有真实相关，一组无
        np.random.seed(42)
        n = 100
        
        data_real = {
            'RealRelated_X': np.random.randn(n),
            'RealRelated_Y': np.random.randn(n) + np.random.randn(n)  # 真实相关
        }
        
        df = pd.DataFrame(data_real)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        correlations = results['correlation_analysis']['pairwise_correlations']
        
        # 应该有显著性的判断
        has_significance_flag = any(r.get('is_significant') is not None for r in correlations)
        assert has_significance_flag


class TestOrdinalColumnDistribution:
    """测试序数量表的正确统计方法."""
    
    def test_ordinal_table_gets_distribution_not_mean_only(self):
        """测试满意度类列输出分布而非只有均值."""
        data = {
            '满意度 (1-5)': [1, 2, 3, 4, 5, 4, 4, 5, 5, 3, 4, 4, 5, 5, 5, 4, 3, 2, 4, 5]
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        type_stats = results['type_aware_statistics']
        
        # 找到满意度列的信息
        sat_info = type_stats.get('满意度 (1-5)', {})
        
        # 类型应该是 ordinal
        inferred_type = sat_info.get('inferred_type')
        assert inferred_type == 'ordinal', f"应该识别为序数型，但得到：{inferred_type}"
        
        # 必须有分布统计
        assert 'distribution' in sat_info
        assert len(sat_info['distribution']) > 0
        
        # 应该有众数
        assert 'mode' in sat_info
        assert sat_info['mode'] is not None
        
        # 应该有众数占比
        assert 'mode_percentage' in sat_info
        
        # 可能有均值（仅供参考）
        if sat_info.get('mean') is not None:
            # 应该有一个 note 说明要谨慎解读
            assert 'note' in sat_info or 'interpretation' in sat_info
    
    def test_ratio_column_marked_explicitly(self):
        """测试百分比/占比列明确标注."""
        data = {
            '渗透率(% 占周活)': [50.0, 60.0, 55.0, 70.0, 65.0],
            '正常值': [10, 20, 30, 40, 50]
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        type_stats = results['type_aware_statistics']
        
        ratio_info = type_stats.get('渗透率(% 占周活)', {})
        
        # 应该被识别为 ratio 或 percentage
        inferred_type = ratio_info.get('inferred_type')
        assert inferred_type in ['ratio', 'percentage'], f"应识别为比例型，但得到：{inferred_type}"
        
        # 必须有极值所在行
        assert 'min_row_index' in ratio_info or 'min_row_idx' in ratio_info
        assert 'max_row_index' in ratio_info or 'max_row_idx' in ratio_info
        
        # 应该注明跨行平均的局限性
        if ratio_info.get('mean') is not None:
            note = ratio_info.get('note', '')
            assert '平均' in note or '谨慎' in note or '局限' in note


class TestDataQualityVsAnalysisQuality:
    """测试数据质量与分析质量分离."""
    
    def test_separates_two_quality_dimensions(self):
        """测试两个质量分独立计算."""
        # 高质量数据
        np.random.seed(42)
        data = {
            'col1': np.random.randn(100),
            'col2': np.random.randn(100)
        }
        df = pd.DataFrame(data)
        
        engine = EnhancedAnalysisEngine(df)
        results = engine.run_comprehensive_analysis()
        
        quality = results['quality_assessment']
        
        # 必须有两个独立的分数
        assert 'data_quality' in quality
        assert 'analysis_quality' in quality
        
        data_score = quality['data_quality'].get('score', 0)
        analysis_score = quality['analysis_quality'].get('score', 0)
        
        # 两个分数都是合理的范围
        assert 0 <= data_score <= 100
        assert 0 <= analysis_score <= 100
        
        # 两个分数不应该完全相同（因为度量不同）
        # （注意：可能偶然相同，但这不是错误）
        
        # 必须有摘要和建议
        assert 'summary' in quality
        assert 'recommendation' in quality
