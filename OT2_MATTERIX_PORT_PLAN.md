# OT-2 → Matterix 移植实施计划（Week 1）

> 来源：2026-08-07 会议（`~/Downloads/dt_week1.txt`）。目标：在 Ubuntu + Isaac Sim 上，用 Matterix 成功实例化 OT-2、让机械臂可动，并用 random-agent 做多实例 sanity check。

## 分工
- **我**：OT-2 本身 —— movement / manipulation（asset config、env、semantics、SM 动作、connector 映射）。
- **Steven**（并行）：周边 assets（瓶子、玻璃器皿等）。规则：每轮只需 USD（`.usda/.usd`），不放 JSON 元数据。

## 可复用起点（Orbit 旧代码）
- `orbit-chem-sim/devel/source/extensions/omni.isaac.contrib_envs/.../chemistry/ot2_pipetting_semantics/` + `ot2_fluid_semantics/`：move OT2 / inhale / exhale 语义。
- `orbit-chem-sim/devel/.../orbit/robots/config/ot2.py`：OT2_CFG（关节、PD、init pose、EE offset、禁用重力）。
- `orbit-chem-assets/data/robots/ot2/`：3 个 USD 变体（LFS 指针，需 Ubuntu `git-lfs pull`）。

## 移植到 Matterix 的模块
| 文件 | 内容 |
|---|---|
| `matterix_assets/robots/ot2.py` | `OT2_CFG` / `OT2_ACTUATED_FRONT_DOOR_CFG`（`MatterixArticulationCfg`），含 action_terms / event_terms / ee_frame sensors |
| `matterix/.../primitive_semantics/ot2/` | `Ot2Storage`（state）、`IsExhaling`/`IsInhaling`（predicates）、`Ot2Exhale`/`Ot2Inhale`（transitions，粒子 API `env_ids` 首参） |
| `matterix_sm/.../move_to_joint_config.py` | 关节空间 primitive action |
| `matterix_sm/robot_action_spaces.py` | `OT2_JOINT_ACTION_SPACE`（4 维物理关节 X/Y/Z/A；B/C 为语义） |
| `matterix_tasks/.../matterix_ot2_liquid_handler.py` | 注册 `Matterix-OT2-LiquidHandler-v1` |
| `matterix-assets-internal/robots/ot2/` | OT-2 USD 指针 + `asset_sha256` 元数据 |

## 关键决策 / 待确认
1. **USD 位置**：私有 `matterix-assets-internal`（推荐，config 保持私有） vs 公共 submodule `Matterix_assets`。→ 待用户决定。
2. **关节命名与对齐**：Orbit USD 只包含 `PrismaticJointMiddleBar / PipetteHolder / LeftPipette / RightPipette` 四个物理关节；connector 的 X/Y/Z/A 通过 `joint_alignment` 做 reference/sign/range 对齐。B/C plunger 不存在于 USD articulation，保留为 liquid semantics。
3. **macOS 约束**：本机离线、无 git-lfs、无 Isaac Sim → 只写代码 + 静态验证；真实验证在 Ubuntu。

## Ubuntu 验证命令（验证时执行）
```sh
cd ~/matterix-internal
export MATTERIX_PATH="$PWD"
git -C source/matterix_assets/data submodule update --init --remote  # 取 OT-2 USD 真实内容
./matterix.sh -p scripts/list_envs.py
./matterix.sh -p scripts/random_agent.py --task Matterix-OT2-LiquidHandler-v1 --num_envs 256 --headless
./matterix.sh -p scripts/random_agent.py --task Matterix-OT2-LiquidHandler-v1 --num_envs 1024 --headless
./matterix.sh -p scripts/run_workflow.py --task Matterix-OT2-LiquidHandler-v1 --workflow move_home
```

## Connector 侧已交付（本机已跑通，offline）

- `matterix/actions_runtime.py` → 关节空间翻译：
  - gantry move 输出 `MatterixMoveToJointConfig`（X/Y + 活动 mount 的 Z/A，含 nozzle/tip 垂直偏移，mm→m）
  - aspirate/dispense 只输出 `LiquidVolume` 语义；当前 USD 没有 B/C plunger 关节
  - 与 `io/digital_twin.py` 的 `_machine_target` 垂直换算保持一致
  - tip pickup/return 使用 safe vertical → XY → contact → 指定 nested child attach/detach → retract
- `matterix/env_cli.py` → `generate` 现在同时写出：
  - `matterix_ot2_actions.py`（`build_ot2_action_cfg`，消费 `TranslatedAction.configs`，joint-space）
  - `matterix_ot2_env.py`（gym 注册 + OT-2、独立 static rack、96-tip nested collection、`pick_and_return_tip` workflow）
- `joint_alignment`、`reference_frame_alignment`、`tip_rack_bindings` 都有严格 schema、候选配置生成与 fail-closed gate。
- 当前准确测试结果以仓库根目录 `uv run pytest -q` 为准；不再保留旧的 261 项快照。

## 当前外部边界（2026-08-14）

- PR47 的 nested-rigid 运行时基础已经可消费，但目前 `IsTipAttached` 一次只选择一个 child。
- PR47 `79efd967` 把 `pipette_tip_mesh_00..95` 作为 96 个 nested children，并把 empty rack 单独实例化；仍需对 exact runtime manifest 验收。
- 当前 checked connector 配置是 RIGHT 8-channel，而 PR47 source-inspected route 是 LEFT/single-child；代码故意拒绝该不一致。
- 实机 X/Y/Z/A、world/base/deck fiducial 与 WebRTC 原生 OT-2 取放证据尚未完成，配置保持 `UNVERIFIED`。

## 待授权：写 matterix-internal / matterix-assets-internal（DT 侧）

需要写以下文件（均在 ~/matterix-internal 与 ~/matterix-assets-internal 的 ot2 分支上）：

1. `source/matterix_assets/matterix_assets/robots/ot2.py` + `__init__.py` 导出
   - `OT2_CFG` / `OT2_ACTUATED_FRONT_DOOR_CFG`（MatterixArticulationCfg，含 action_terms/event_terms/sensors）
2. `source/matterix/matterix/managers/semantics/primitive_semantics/ot2/`（Ot2Storage/IsInhaling/IsExhaling/Ot2Inhale/Ot2Exhale）
3. `source/matterix_sm/matterix_sm/primitive_actions/move_to_joint_config.py` + `__init__.py` + `robot_action_spaces.py` 加 `OT2_JOINT_ACTION_SPACE`
4. `source/matterix_tasks/matterix_tasks/test_dev_tasks/matterix_ot2_liquid_handler.py` + `__init__.py` 注册 gym task
5. `~/matterix-assets-internal/robots/ot2/`（USD 指针/README，含 asset_sha256 元数据）

## 完成定义（sanity check）
- [ ] OT-2 成功实例化进 scene（无 visual/collider 报错）
- [ ] arm/EE 可动（关节目标可达，观测 joint_pos/ee 变化）
- [ ] `random_agent.py` 正常跑 `--num_envs 256` 和 `1024`
- [ ] 各 env 动作独立（不被同步）；reset 时 joint 随机化生效
