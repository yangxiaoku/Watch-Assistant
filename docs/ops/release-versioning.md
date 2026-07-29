# 发布物版本规范

发布包只能从干净且已提交的 Git 工作树生成。运行 `scripts/build_release.sh` 后，包名为
`watch-assistant-<hash7>-<yyyymmdd-HHMM>.tar.gz`，其中 hash 是当前 `HEAD` 的短提交号。

包内根目录包含 `VERSION`，记录 `commit`、UTC `build_time` 和 `branch`。默认产物目录是
`release-archive/<UTC 日期>/`；该目录只用于本地或发布流水线归档，不提交到 Git。

脚本拒绝带有未提交修改或未跟踪文件的工作树。历史包不能伪装成当前发布物，重新打包必须
产生新的 commit 绑定文件名。
