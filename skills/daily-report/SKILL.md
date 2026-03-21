---
name: daily-report
description: >
  ローカルログを集約し、その日全体の活動を自分用または共有用の日報ドラフトに再構成する。
  今日の自分用日報、共有用レポート、昨日の活動まとめを作りたい時に使う。
user-invocable: true
---

# Daily Report

その日のローカルログから、date-first で日報ドラフトを組み立てる。
主目的は「その repo で何をしたか」ではなく、「その日全体で何をしていたか」を再構成すること。

## Goal

- 1日全体の活動を、日本語の日報ドラフトとして 3-6 項目に再構成する
- `workspace default` ではなく `date-first default + optional workspace filter` として扱う
- `自分用` と `共有用` の 2 モードで出し分ける
- mode が分かれば ask せず、分からない時だけ入口で 1 回だけ確認する
- low confidence は本文内の注記で扱い、途中で追加 ask せず完走する

## Inputs

- 対象日
  - 指定がなければ `today`
  - 単日指定を基本とし、必要なら `YYYY-MM-DD` を使う
- mode
  - `自分用`
  - `共有用`
- workspace
  - 任意
  - 主軸ではなく補助フィルタ
  - 特定 workspace の git / file 根拠を強めたい時だけ使う
  - 現状の source 実装では、workspace を指定しても `claude-history` / `codex-history` / `chrome-history` はその日全体のログを返しうる
  - したがって strict な repo 限定指定ではなく、mixed-scope の内訳を制御する補助情報として扱う

## Entry Contract

入力は自然言語抽出と引数なし実行の 2 経路を前提にする。

### 自然言語からの抽出

- 「今日の自分用日報」「昨日の共有用レポート」などから日付と mode を抽出する
- 「daytrace の日報」「`/path/to/repo` での日報」などから workspace を抽出する
- mode が自然言語から抽出できた場合は ask しない

### 引数なし実行

- mode が取れなかった場合だけ、最初の 1 ターンで 1 問だけ確認する
- 確認文面は `自分用ですか？ 共有用ですか？`
- 日付は `today` を使う
- workspace は未指定のまま date-first で進める

### 追加 ask の禁止

- 原則として入口以外では質問しない
- 途中で source 欠損や low confidence が見えても追加 ask しない
- 入口で取れなかった情報はデフォルト値で埋める

## Auto-trigger Contract

この節は `daytrace-session` のような orchestration が、この skill を自動起動する時の契約を定義する。
個別実行時の UX を変えるものではなく、いつ自動で呼んでよいかだけを明文化する。

- `mode` が明示されている場合は、その mode を最優先し、自動補完しない
- `mode` が未指定の場合、orchestration はまず `自分用` を既定として生成してよい
- `mode` が未指定かつ `summary.total_groups >= 5` の場合に限り、orchestration は `共有用` も追加生成してよい
- `summary.total_groups < 5` の場合、orchestration は `自分用` のみを生成する
- 共有用生成時も、この skill の `Escalation Conditions` は有効であり、機密境界の確認が必要ならその条件を優先する

### Escalation Conditions

以下の例外条件でのみ、入口直後（データ収集前）の確認を入れてよい:

- **共有用で all-day スコープのみ、workspace ログなし**: 「共有用日報に個人端末の全日ログ（Chrome 履歴等）が含まれますが、このまま進めますか？」と 1 回だけ確認。理由: 機密境界の判断はユーザーに委ねるべき
- それ以外は ask せずに degrade して進む

## Data Collection

必ず最初に `daily_report_projection.py` を 1 回だけ実行し、中間 JSON を取得する。

この adapter は shared derived data を優先して読み、該当 slice が store に無い場合だけ内部で `aggregate.py` を 1 回実行して hydrate する。
返却 JSON の主要 shape は `aggregate.py` 互換で、`sources` / `timeline` / `groups` / `summary` をそのまま読める。

`aggregate.py` はこの `SKILL.md` と同じ plugin 内の `scripts/` ディレクトリにある。
この `SKILL.md` のあるディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

date-first デフォルト:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily_report_projection.py --date today --all-sessions
```

特定日:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily_report_projection.py --date 2026-03-09 --all-sessions
```

workspace の git / file 根拠を current repo に固定したい場合:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily_report_projection.py --date today --all-sessions --workspace /absolute/path/to/workspace
```

この指定の意味:

- `git-history` と `workspace-file-activity` は `--workspace` で絞り込まれる
- `claude-history` / `codex-history` は `--all-sessions` が付くと workspace を無視する
- `chrome-history` は現状常に workspace を無視する
- したがって downstream の生成では `sources[].scope` を見て、repo ローカルの根拠と全日根拠を混同しない

中間 JSON の主な読みどころ:

- `sources`: source ごとの `success / skipped / error / scope`
- `timeline`: 時系列イベント
- `groups`: 近接イベントを束ねた活動グループ
- `summary`: 件数と source 利用状況
- `report_date` / `output_dir`: 単日スコープ時に付く。Layer 3 artifact の保存先（`docs/output-polish.md` §7）

## Persisted artifacts（Layer 3）

`output_dir` が非 null のとき、生成した日報 Markdown をファイルに書き出す（チャットに全文を載せ切らない）。

- `report-private.md` — 自分用日報
- `report-share.md` — 共有用を生成した場合のみ（出さない場合はチャットで「共有用は未生成」と明示）
- 親ディレクトリが無ければ作成してから保存する（`daily_report_projection` は `output_dir` を返す前にコード側でも作成する。Write 前の再保証として同様の扱いでよい）
- チャットには **要約・絶対パス・ファイル単位の成功/失敗** だけを返す

## Scope Contract

この skill は date-first だが、source には `all-day` と `workspace` の 2 種類がある。

- `all-day`
  - その日全体を代表するログ
  - 例: `claude-history`, `codex-history`, `chrome-history`
- `workspace`
  - 指定 workspace または current working directory に依存するログ
  - 例: `git-history`, `workspace-file-activity`

このため、workspace 未指定でも出力は完全な単一スコープにはならず、全日ログと cwd 起点の workspace ログが混在しうる。
workspace 指定時も mixed-scope は解消されず、repo ローカルの根拠密度が上がるだけで `all-day` ログまで strict な repo filter にはならない。
workspace は date-first の主軸ではなく補助フィルタだが、mixed-scope を隠さないこと。

## Execution Rules

1. `daily_report_projection.py` を 1 回だけ実行する
2. 先に `sources` を読み、取得できた source と `scope` を把握する
3. 次に `groups` を優先して読み、必要に応じて `timeline` を補助参照する
4. 活動項目は 3-6 個に絞り、「その日何を進めたか」が伝わる粒度に再構成する
5. `git-history + claude/codex-history` が重なるグループを主要活動候補として優先する
6. browser 履歴は文脈補助として使い、単独では主項目を作りすぎない
7. `workspace-file-activity` だけで意味が確定しない場合は「作業痕跡」として控えめに表現する
8. workspace 指定があっても `all-day` source を repo 限定の根拠として扱わない
9. source が欠けていても止まらず、分かる範囲で出す
10. low confidence は本文内注記で処理し、確認セクションへ分離しない
11. 途中で追加 ask しない

## Output Rules

出力は日本語 Markdown。
mode によって構成と文体を変えるが、どちらも date-first の日報として出す。

### Canonical timeline（`docs/output-polish.md` §5-1）

- 活動の並べ方は **時系列（古い→新しい）** を基本にする。時間帯ラベル（午前/午後等）は必須としない
- 共有用でもカテゴリ内の順序がログ時刻と明らかに矛盾しないようにする
- 同一出来事の重複記述を避け、ユーザー視点の 1 本の時間軸でまとめる
- 推測を含む場合は推測であることを明示する

### 共通ルール

- 先頭は必ず `## 日報 YYYY-MM-DD`
- 活動項目は 3-6 個
- 根拠の扱いは mode で異なる:
  - 自分用: 各項目に `根拠:` を inline で付ける。根拠は `[source 名] の [内容]` の形式
    - 例: `根拠: git の commit ログ "Add source registry drop-in support" と、Claude の会話ログ "drop-in の設計を議論"`
  - 共有用: 本文には根拠を書かない。日報末尾に `### 参考: 根拠一覧` セクションを設け、項目番号と根拠を対応付けて一括記載する
- 同じ内容を重複して書かない
- `未完了の手がかり` をログから読み取れた分だけ付ける（0 件可）
- 手がかりがなければ「ログからは未完了の手がかりを特定できませんでした」で閉じる
- `確認したい点` セクションは作らない
- evidence が直接示唆しない推論は控える（事実膨張ガード）
- 本文の語彙は行動レベルに変換し、実装名をそのまま列挙しない
  - `skill_miner_prepare.py を +65/-21 修正` → `パターン抽出の準備ロジックを修正`
  - `ISSUE-observation-contract-unification.md` → `観測コントラクトの統合作業`
  - `aggregate.py を修正` → `ログ集約処理を改善`
  - 固有名（ファイル名、commit hash、diff 行数）は根拠に退避し、本文では意味だけ出す
- 根拠は短文化する
  - `git commit 7daded3c（+65/-21）` → `Git の修正履歴`
  - `codex-history「P1 → P2 → ... 着手して」` → `AI 会話ログでの修正方針`
  - `Chrome 閲覧ログ https://example.com/...` → `ブラウザでの調査ログ`
  - commit hash、差分行数、生の会話引用はできるだけ直接出さない

### 自分用

自分だけが後で読み返して思い出せることを優先する。

- 構成: 時系列ベース
- 文体: メモ的な文体でよい。「〜した」「〜だった」のような簡潔な表現を使う
- 語彙: メモ的でよい。省略可
- 文量: 各項目 1-3 文
- 根拠: 各項目に `根拠:` を inline で付ける
- 未完了の扱い: 途中 / TODO / 詰まりをそのまま残してよい
- 文脈補足: 最小限でよい

推奨構成:

```markdown
## 日報 YYYY-MM-DD

### 今日の流れ
1. 見出し
   内容: 1-3文
   根拠: git の commit ログ "xxx" と Claude の会話ログ "yyy"

### 未完了の手がかり
- ログから読み取れた分だけ（0 件可）
```

### 共有用

第三者が読んで、その日の成果と残課題を把握できることを優先する。

- 構成: カテゴリベース
- 文体: ですます調を使う。「〜した」「〜だった」ではなく「〜しました」「〜でした」で書く
- 語彙: 第三者が読める表現
- 文量: 各項目 2-4 文
- 根拠: 本文には書かない。日報末尾の `### 参考: 根拠一覧` にまとめる
- 未完了の扱い: 成果と残課題を分けて書く
- 文脈補足: 「なぜやったか」を 1 文添える
- 主従の編集: 中心テーマを 1-2 本に絞り、他は補助的な出来事として扱う
  - 主要活動: 「今日の中心作業」として前に出す
  - 補助活動: 「周辺整理」「次フェーズ準備」として控えめに扱う
  - 全項目を同じ重さで並べない

推奨カテゴリ:

- `実装`
- `調査`
- `設計 / 判断`
- `未完了の手がかり`

推奨構成:

```markdown
## 日報 YYYY-MM-DD

### 今日の概要
- 1-2文で全体要約（ですます調）

### 実装
- 見出し
  - 内容: 2-4文（ですます調）
  - 成果:
  - 残課題:

### 調査
- 必要なら追加

### 設計 / 判断
- 必要なら追加

### 未完了の手がかり
- ログから読み取れた分だけ（0 件可）

### 参考: 根拠一覧
- 実装-1: git の commit ログ "xxx" と Claude の会話ログ "yyy"
- 調査-1: Chrome の閲覧ログ、Codex の会話ログ "zzz"
```

## Mixed-Scope Note Rules

成功した `sources[]` の `scope` を見て、注記の要否を決める。

- `all-day` と `workspace` の両方が含まれる場合
  - artifact 本文への mixed-scope 注記は **任意**（読みやすさ優先）。`daytrace-session` のセッション完了チャットでは §5-5 として **必須**（orchestration 側で出す）
  - artifact に入れる場合は冒頭 1 行で scope を伝える例: `この日報は、1日のログと workspace ローカルの変更ログをもとに再構成しています。`
  - 詳細な source 別 scope 説明は日報末尾のフッターに置いてもよい
  - フッター例: `> 再構成元: git-history, claude-history, codex-history / workspace ローカルの変更ログは {workspace名} に限定されています`
- `all-day` のみ、または `workspace` のみの場合
  - mixed-scope 注記は必須ではない
- 注記は事実説明に留める
  - 「この日報は不完全です」と過度に弱めない
  - ただし coverage を誤認させる表現は避ける

## Confidence Handling

`確認したい点` セクションは使わず、根拠の具体性で信頼度を表現する。
`Confidence: high` / `Confidence: medium` のようなラベル明示は行わない。
弱い箇所だけ注記を残す。

- `high`
  - そのまま本文に採用する。根拠が具体的なら信頼度は自然に伝わる
- `medium`
  - 本文に採用してよい
  - 断定しすぎず、必要なら `と見られる` `中心だった` などの表現にする
- `low`
  - 本文に入れる場合は inline 注記にする
  - 例:
    - `注記: ファイル変更からは確認できるが、最終的な意図は断定できない`
    - `注記: ブラウザログのみからの補助推定です`
  - low confidence だけの独立セクションは作らない
  - low confidence を理由に追加 ask しない

### 低信頼度データの使用ポリシー

- 低信頼度データ（Chrome 履歴、workspace-file-activity 単独）は「裏付け・補助」として使い、それ単独で主要根拠にしない
- 同じ低信頼度データを複数項目で繰り返さない（一つの項目につき最大一回）
- 低信頼度データ単独で独立した活動項目を構成しない
- 高信頼度ソース（Git、AI 会話ログ）とペアになるときだけ採用し、「補助推定」と明示する
- 原則: 低信頼度データは「一回・補助・裏方」

## Graceful Degrade

source 欠損の判定は `summary` と `sources` から行う。

- `source_status_counts.success == 0`
  - `source が 0 本` とみなす
- `source_status_counts.success` が 1-2
  - `source が 1-2 本だけ` とみなす
- `sources[].status` に `skipped` / `error` があっても、成功 source が残っていれば継続する
- 注記: `summary.no_sources_available` は空結果を示すメタ情報であり、実際の分岐判定は `source_status_counts.success` を優先する

### source が 0 本

以下のような空日報を返す。

```markdown
## 日報 YYYY-MM-DD

### 今日の概要
- 利用可能なローカルログが見つからなかったため、自動生成できる情報はありませんでした。

### 未完了の手がかり
- Git、Claude/Codex、Chrome など少なくとも 1 系統のログが取れる状態で再実行する
```

### source が 1-2 本だけ

- 簡易日報として返す
- `取得できたログは限定的` と冒頭に明記してよい
- 断定的な振り返りを避ける
- それでも mode に応じた構成は維持する
- low confidence は本文内注記で処理する

## Sample Output

### 自分用

```markdown
## 日報 2026-03-11

この日報は、1日のログと workspace ローカルの変更ログをもとに再構成しています。

### 今日の流れ
1. aggregate の `scope` 追加まわりを確認して、daily-report 側の書き換え方針を固めた。
   根拠: Codex の会話ログ "scope の仕様確認"、git の commit ログ "Add scope field to sources"

2. `daily-report` を workspace 前提から date-first 前提へ直し、入口 ask と mode 差分を整理した。
   根拠: workspace-file-activity の SKILL.md 編集、Codex の会話ログ "文言調整"

3. mixed-scope 注記の文面は入れたが、実データでどの source が強く出るかはまだ観察中。
   根拠: Chrome の閲覧ログ、workspace-file-activity の編集痕跡
   注記: ブラウザログのみからの補助推定です

### 未完了の手がかり
- sample output と fixture 表現を見比べて wording を詰める（Codex の会話ログに "fixture はまだ" の発言あり）
- mixed-scope 注記が長すぎる場合は短縮版を作る（commit の TODO コメントに記載あり）

> 再構成元: git-history, codex-history, chrome-history, workspace-file-activity / workspace ローカルの変更ログは daytrace に限定
```

### 共有用

```markdown
## 日報 2026-03-11

この日報は、1日のログと workspace ローカルの変更ログをもとに再構成しています。

### 今日の概要
- 日報 skill を date-first 前提へ再整理し、共有向けに読める mode 契約と mixed-scope 注記ルールを明文化しました。

### 実装
- daily-report の仕様を `workspace default` から `date-first default + optional workspace filter` へ更新しました。
  - 成果: 対象スコープ、入口 ask、confidence の扱いを 1 つの契約として読み取れる形に整理しました。
  - 残課題: 実データを使った wording の最終確認は別途必要です。

### 設計 / 判断
- `自分用` と `共有用` の差を、構成・語彙・未完了の扱いまで分けて定義しました。
  - 成果: 共有用では背景説明と成果 / 残課題の分離を必須にしました。
  - 残課題: 実際の生成文がこの差分を安定して守れるかは運用確認が必要です。

### 調査
- mixed-scope の説明は `sources[].scope` を見て自動で注記する前提に整理しました。
  - 成果: coverage の誤認を避けつつ、date-first の価値を落とさないルールにしました。
  - 残課題: 一部の補助ログは意図を断定できないため、本文内注記で扱います。
  - 注記: ブラウザログのみからの補助推定です

### 未完了の手がかり
- mixed-scope 注記を fixture ベースでレビューする（commit の TODO コメントに記載あり）
- README / demo 側の文言と整合させる

### 参考: 根拠一覧
- 実装-1: Git の commit ログ "Refactor daily-report to date-first"、Codex の会話ログ "仕様整理"
- 設計/判断-1: SKILL.md の mode 定義、workspace-file-activity の更新痕跡
- 調査-1: aggregate.py の `sources[].scope`、sources.json の scope_mode

> 再構成元: git-history, codex-history, chrome-history, workspace-file-activity / workspace ローカルの変更ログは daytrace に限定
```

## Completion Check

以下を満たすまで出力を確定しない。

- `daily-report` が date-first skill として一貫して説明されている
- workspace が補助フィルタとして説明されている
- workspace 指定が strict repo filter ではないと読める
- `自分用` と `共有用` の差が構成・語彙・未完了の扱いまで見える
- mode が自然言語から取れた場合は ask しないと読める
- mode が取れない場合だけ入口で 1 回 ask すると読める
- 途中で追加 ask しないと読める
- `確認したい点` 依存の旧フローが残っていない
- mixed-scope 注記ルールが `sources[].scope` ベースで一意に読める
- source 欠損時も空日報または簡易日報で完走する

この skill は、収集から日報ドラフト生成までを 1 コマンドで完走させる前提で使う。
