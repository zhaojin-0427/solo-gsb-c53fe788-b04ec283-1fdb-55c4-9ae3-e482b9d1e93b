# 多标注语料冲突裁决台 — 应用版本号。
# 报告生成时会记录此版本号与求解器版本，旧报告永不被重算/改写。
APP_NAME = "多标注语料冲突裁决台"
APP_VERSION = "1.0.0"

# 候选得分 = 重叠比例(Jaccard) × 标注者权重，以该因子放大为整数交给 CP-SAT，
# 保证求解器只处理整数目标，同时数值误差被限定在 1/1000 以内。
SCORE_SCALE = 1000

# CP-SAT 确定性求解参数（固定随机种子、固定线程数与时间上限）。
SOLVER_PARAMS = {
    "random_seed": 20261001,
    "num_search_workers": 1,
    "max_time_in_seconds": 10.0,
}
