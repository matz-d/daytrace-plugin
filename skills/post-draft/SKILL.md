---
name: post-draft
description: >
  1日の活動ログから、その日全体の流れを読者向け narrative draft に再構成する。記事を書きたい、ブログにまとめたい、ふりかえりを書きたい、学びを共有したい時に使う。topic / reader は任意で上書きできる。
user-invocable: true
---

# Post Draft

その日のローカルログから、date-first で narrative draft を組み立てる。
主目的は媒体を選ぶことではなく、その人だけが書ける一次情報ベースの `Context & Narrative` を下書き化すること。

## Goal

- 1 日全体の活動ログから、公開前の narrative draft を 1 本組み立てる
- `workspace default` ではなく `date-first default + optional workspace filter` として扱う
- 入口 ask なしで完走し、必要なら `topic` と `reader` だけ optional override として受け付ける
- 読者に応じてトーン、構成、説明粒度を自動で切り替える
- source が欠けていても narrative が破綻しない短縮版を返す

## Inputs

- 対象日
  - 指定がなければ `today`
  - 単日指定を基本とし、必要なら `YYYY-MM-DD` を使う
- reader
  - 任意
  - 未指定時は自動推定する
- topic
  - 任意
  - 未指定時は narrative policy で自動選定する
- workspace
  - 任意
  - 主軸ではなく補助フィルタ
  - 特定 workspace の git / file 根拠を強めたい時だけ使う
  - 現状の source 実装では、workspace を指定しても `claude-history` / `codex-history` / `chrome-history` はその日全体のログを返しうる
  - したがって strict な repo 限定指定ではなく、mixed-scope の内訳を制御する補助情報として扱う

## Entry Contract

入力は自然言語抽出と引数なし実行の 2 経路を前提にする。

### 自然言語からの抽出

- 「今日の記事を書きたい」「昨日の学びをブログ向けにまとめたい」などから日付を抽出する
- 「非エンジニア向けに」「個人ブログ向けに」などから `reader` を抽出する
- 「aggregate.py の話で」「scope の変更について」などから `topic` を抽出する
- 「daytrace の」「`/path/to/repo` で」などから workspace を抽出する

### 引数なし実行

- ask は 0 回を基本とする
- 日付は `today` を使う
- `reader` は自動推定する
- `topic` は narrative policy で自動選定する
- workspace は未指定のまま date-first で進める

### 曖昧性解消 Ask（`docs/output-polish.md` §10）

- 基本は ask なしで完走する
- **例外**: 公開範囲・主軸の特定・内輪情報の伏せ方など、曖昧なままだと**品質または正確性が明らかに落ちる**場合に限り、**この skill について最大 2 ターン**まで確認してよい
- 抽象的な文体（技術寄り/振り返り寄り等）だけを選ばせる Ask は禁止
- 無回答時はフォールバックで続行する
- `Escalation Conditions` の機密確認は従来どおり別枠

### 追加 ask の禁止（原則）

- 上記例外と `Escalation Conditions` を除き、入口でも途中でも質問しない
- source 欠損や low confidence が見えても追加 ask しない（ただし §10 例外は可）
- 抽出できなかった情報はデフォルト値で埋める

## Auto-trigger Contract

この節は `daytrace-session` のような orchestration が、この skill を自動起動する時の契約を定義する。
個別実行時の UX を変えるものではなく、いつ自動で呼んでよいかだけを明文化する。

- orchestration は、次の条件のいずれかを満たす場合にこの skill を自動起動してよい
- 条件 1: `sources` に `git-history` と (`claude-history` または `codex-history`) が両方 `success` で含まれる
- 条件 2: `summary.total_groups >= 4`
- どちらの条件も満たさない場合、orchestration はこの skill の自動起動をスキップする
- `topic` や `reader` の override が明示されている場合はそれを優先し、自動起動条件は呼び出し可否の判断にのみ使う

## Data Collection

必ず最初に `post_draft_projection.py` を 1 回だけ実行し、中間 JSON を取得する。

この adapter は shared derived data を優先して読み、該当 slice が store に無い場合だけ内部で `aggregate.py` を 1 回実行して hydrate する。
返却 JSON の主要 shape は `aggregate.py` 互換で、`sources` / `timeline` / `groups` / `summary` をそのまま読める。cached `patterns` があれば追加で添付される。

`aggregate.py` はこの `SKILL.md` と同じ plugin 内の `scripts/` ディレクトリにある。
この `SKILL.md` のあるディレクトリから `../..` を辿った先を `${CLAUDE_PLUGIN_ROOT}` として扱う。

date-first デフォルト:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/post_draft_projection.py --date today --all-sessions
```

特定日:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/post_draft_projection.py --date 2026-03-09 --all-sessions
```

workspace の git / file 根拠を current repo に固定したい場合:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/post_draft_projection.py --date today --all-sessions --workspace /absolute/path/to/workspace
```

この指定の意味:

- `git-history` と `workspace-file-activity` は `--workspace` で絞り込まれる
- `claude-history` / `codex-history` は `--all-sessions` が付くと workspace を無視する
- `chrome-history` は現状常に workspace を無視する
- したがって downstream の生成では `sources[].scope` を見て、repo ローカルの根拠と全日根拠を混同しない

中間 JSON の主な読みどころ:

- `sources`: source ごとの `success / skipped / error / scope`
- `groups`: 近接イベントを束ねた活動グループ
- `timeline`: 詳細な時系列
- `summary`: 件数と source 利用状況
- `report_date` / `output_dir`: 単日スコープ時に付く（`docs/output-polish.md` §7）

## Persisted artifacts（Layer 3）

`output_dir` が非 null のとき、生成した narrative を `post-draft.md` に保存する。チャットには要約・パス・成否のみ返す。

- 親ディレクトリが無ければ作成してから保存する（`daily-report` と同様。`daily_report_projection` / `post_draft_projection` は返却前に `output_dir` をコード側でも用意する）

## Scope Contract

この skill は date-first だが、source には `all-day` と `workspace` の 2 種類がある。

- `all-day`
  - その日全体を代表するログ
  - 例: `claude-history`, `codex-history`, `chrome-history`
- `workspace`
  - 指定 workspace または current working directory に依存するログ
  - 例: `git-history`, `workspace-file-activity`

workspace 未指定でも、出力は全日ログと cwd 起点の workspace ログが混在しうる。
workspace 指定時も mixed-scope は解消されず、repo ローカルの根拠密度が上がるだけで `all-day` ログまで strict な repo filter にはならない。
date-first の narrative を組み立てつつ、mixed-scope を隠さないこと。

## Narrative Policy

主題選定は Python helper に切り出さず、この `SKILL.md` の policy として実装する。
`aggregate.py` が返す `groups` / `events` を読み、主題選定と narrative 構成を一体で行うこと。

### 主題選定の優先順位

`--topic` が明示されている場合は、それを最優先する。
未指定時は `groups` から以下の 3 段フォールバックで主題を 1 つ選ぶ。

#### 優先度 1: AI + Git 共起グループ

- 条件: `sources` に `git-history` と (`claude-history` または `codex-history`) が両方含まれる
- 複数該当時: `event_count` が最大の group を選ぶ
- 根拠: AI との対話と実際のコミットが同時間帯にある group は、実作業の密度が最も高い

#### 優先度 2: AI 密度グループ

- 条件: `confidence_categories` に `ai_history` を含み、かつ group 内の `claude-history` / `codex-history` イベント数が 3 件以上
- 複数該当時: AI イベント数が最大の group を選ぶ
- 根拠: AI との対話が集中している group は、試行錯誤の narrative を組み立てやすい

#### 優先度 3: 最大イベント数グループ

- 条件: 上記に該当しない場合
- 選び方: `event_count` が最大の group を選ぶ
- 根拠: 補助ログしかなくても、その日の中心的な活動塊を最低限拾う

### 主題の広げ方

- 選んだ group を narrative の中心に据える
- 周辺 group は背景、前提、判断、次の一手として補助的に接続する
- 主題が 1 つでも、本文は単なる group 要約にしない
- `events[].summary` や `type` から、転換点、詰まり、判断理由、学びを narrative 構成で拾う
- 「学びの転換点」の判定は決定論的 helper ではなく LLM の narrative 構成フェーズで行う

### 転換点の拾い方

narrative では以下のどこが今日の転換点だったかを、本文で明示的に拾う。

- 何を直していたつもりだったか
- どこで別の問題や視点が見えたか
- なぜ重心が移ったのか（例: 午前は実装、夕方はハッカソン準備へ）
- 最後に何が残ったか

転換点は説明的につなげるのではなく、「ここで流れが変わった」と読者に伝わる narrative の起伏として描く。

## Reader Policy

優先順位は `--reader` override > 自然言語から抽出した reader > デフォルト読者 とする。
ただし `Escalation Conditions` の判定はデフォルト読者の適用前に行う。

### デフォルト読者

- 自然言語から `reader` を抽出できた場合はそれを使う
- 抽出できず、`--reader` override も無く、かつ escalation 判定が発火しない場合のデフォルトは `同じ技術スタックを使う開発者`

### `--reader` override

- `reader` が明示されている場合は、その読者像に合わせてトーンと粒度を調整する
- 例: `--reader "社内の非エンジニア"` の場合、技術用語を減らし、背景、プロセス、成果を中心に書く
- 例: `--reader "個人ブログの読者"` の場合、一人称で試行錯誤や学びのストーリーを前に出す

### 自動判定ルール

ask は使わず、読者と主題から以下を自動で決める。

- トーン
  - 技術者向け: 具体的、再現可能、実装寄り。ですます調で書く
  - 非技術者向け: プロセス、判断、成果中心。技術用語は必要最小限に言い換える。ですます調で書く
  - 個人ブログ向け: 一人称、試行錯誤、学びの転換点を前に出す。ですます調で書く
- 構成
  - 基本: 背景 / 今日の中心 / 気づき の 3 セクション
  - 詰まった点・判断・学びは固定セクションにせず、narrative 内で evidence があれば自然に触れる
  - 調査系主題（以下の条件を満たす場合に限り、動機 / 比較したもの / 判断 / 結論 の 4 セクション構成に切り替えてよい）:
    - 中心 group の evidence が `chrome-history` 主体で git commit がほぼない（実装ではなく調査が中心）
    - group の summary や events に複数の選択肢・ツール・アプローチの比較が読み取れる
    - 上記 2 条件を両方満たさない場合は基本の 3 セクション構成を使う
- 長さ
  - 300-1200 字を基本とする
  - source が薄い日は短くしてよい（300 字でも成立させる）
  - reader が非技術者寄りの場合は背景説明を少し厚くし、詳細実装は削る

## Execution Rules

1. `post_draft_projection.py` を 1 回だけ実行する
2. 先に `sources` を読み、取得できた source と `scope` を把握する
3. 次に `groups` を読み、主題選定の 3 段フォールバックで中心 group を決める
4. 必要に応じて `timeline` を補助参照し、背景や前後関係を補う
5. workspace 指定があっても `all-day` source を repo 限定の根拠として扱わない
6. narrative は 1 本通った話として組み立てる
7. `chrome-history` は補助的文脈として使い、単独では主題化しすぎない
8. `workspace-file-activity` 単独の場合は「作業痕跡」として控えめに扱う
9. 事実と推定を分け、confidence が低い内容は過剰に膨らませない
10. 公開・送信はせず、下書きだけ返す
11. `team-summary` / `slack` を main UX に戻さない
12. evidence が弱い日は「気づき」セクションを省略または短縮してよい
13. evidence が直接示唆しない推論は控える（事実膨張ガード）

## Output Rules

出力は日本語 Markdown。
少なくとも以下の要素を含む narrative draft として返す。

- `# タイトル案`
- 背景（なぜこの作業をしていたか）
- 今日の中心（何を進めたかの narrative。詰まり・判断があれば自然に含める）
- 気づき（evidence から読み取れた学びや次の手がかり。evidence が弱ければ省略・短縮可）

### 見出し構造（`docs/output-polish.md` §9-3）

標準:

```markdown
# タイトル案（8–20 字）
## 背景
## 今日の中心
## 気づき（根拠が弱い場合は省略可）
```

非技術者向け・調査中心の別構成は `Reader Policy` のテンプレートに従う。

### 禁止語・プロダクトコピー

次は使わない: **寄り道**、**今日の重心**（置換: 「別の作業をした時間」「今日の中心作業」など、行動レベルの表現）。

### 共通ルール

- 文体: ですます調を基本とする。「〜した」「〜だった」ではなく「〜しました」「〜でした」で書く
- reader が誰であっても丁寧語で書く
- 1 本の narrative として読み通せる流れにする
- group の列挙で終わらせず、背景と意味づけを入れる
- source 名やファイル名は必要な範囲で本文に出してよい
- 根拠が薄い箇所は断定しない
- `確認したい点` セクションは作らない
- low confidence は本文内の注記で処理する
- 読者に合わせて専門用語の量と説明密度を変える
- evidence が示唆しないことは書かない
- 本文の語彙は行動レベルに変換し、実装名をそのまま列挙しない
  - `skill_miner_prepare.py` → `パターン抽出の準備段階`
  - `ISSUE-observation-contract-unification.md` → `観測コントラクトの統合作業`
  - `worktree_status snapshot` → `作業ディレクトリの状態記録`
  - 固有名は narrative に必要な範囲でのみ出し、読者が立ち止まらない語彙を使う
- 根拠は短文化し、本文の narrative を壊さない
  - commit hash、差分行数、生の会話引用は本文に直接出さない
  - 必要なら「Git の修正履歴から」「AI との設計検討で」のように要約する
- 「気づき」は一般論で終わらせず、今日の具体的な出来事から導いた学びにする
  - `「ドキュメントと実装の乖離は LLM に直結する」` → 何がズレていて、何が不安定になったかまで半歩具体的に

### タイトル案のルール

- 初見読者が「なぜそれが重要か」をタイトル単体で感じ取れること
- 内部プロジェクト名やコンポーネント名だけのタイトルは避ける
  - `DayTrace の心臓部を直す：observation contract 統合` → 内輪寄り
  - `AI エージェントが "過去の自分" を正確に読めるようになるまで` → 意味が伝わる
- 8-20 字程度の present/past-tense phrase で、読んでみたい理由がある表現にする

### 技術者向けの基本構成

```markdown
# タイトル案

## 背景

## 今日の中心

## 気づき
```

「詰まった点」「判断したこと」は固定セクションにせず、「今日の中心」の narrative 内で evidence があれば自然に触れる。
「気づき」が evidence から読み取れない日は、「今日は実装に集中した日だった」のように正直に短く閉じてよい。

### 非技術者向け override の構成

```markdown
# タイトル案

## 何に取り組んだか

## 今日の中心

## 気づき
```

## Mixed-Scope Note Rules

成功した `sources[]` の `scope` を見て、注記の要否を決める。

- `all-day` と `workspace` の両方が含まれる場合
  - artifact 本文への mixed-scope 注記は **任意**。`daytrace-session` のセッション完了チャットで §5-5 が必須
  - artifact に入れる場合は冒頭 1 行の例: `この下書きは、1日のログと workspace ローカルの変更ログをもとに再構成しています。`
  - 詳細な source 別 scope 説明は narrative 末尾のフッターに置いてもよい
- `all-day` のみ、または `workspace` のみの場合
  - mixed-scope 注記は必須ではない
- 注記は coverage の誤認を防ぐための事実説明に留める
  - narrative の価値を過度に弱めない

## Confidence Handling

根拠の具体性で信頼度を表現する。`Confidence: high` のようなラベル明示は行わない。
弱い箇所だけ注記を残す。

- `high`
  - そのまま narrative に採用する。根拠が具体的なら信頼度は自然に伝わる
- `medium`
  - narrative に採用してよい
  - 必要なら `と見られる` `中心だった` などで断定を弱める
- `low`
  - narrative に入れる場合は inline 注記にする
  - 例:
    - `注記: ファイル変更からは確認できるが、最終的な意図は断定できない`
    - `注記: ブラウザログのみからの補助推定です`
  - 別セクションへ分離しない
  - low 根拠だけを理由にした追加 ask はしない（§10 の品質 Ask は別）

### 低信頼度データの使用ポリシー

- 低信頼度データ（Chrome 履歴、workspace-file-activity 単独）は「裏付け・補助」として使い、それ単独で narrative の主要根拠にしない
- 同じ低信頼度データを複数箇所で繰り返さない（使うなら一回まで）
- 低信頼度データ単独で narrative の主題や転換点を構成しない
- 高信頼度ソース（Git、AI 会話ログ）とペアになるときだけ採用する
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

以下のような空 narrative を返す。

```markdown
# タイトル案

## 背景
利用可能なローカルログが見つからず、その日の活動から narrative を組み立てられなかった。

## 今日の中心
今日は組み立てに使えるログが不足していたため、中心的な活動を再構成できなかった。

## 気づき
少なくとも 1 系統のログが取れる状態で再実行すると、下書きを組み立てやすくなる。
```

### source が 1-2 本だけ

- 短縮版 narrative として返す
- `取得できたログは限定的` と導入で明記してよい
- 断定的なストーリー化を避ける
- それでも `タイトル / 背景 / 今日の中心 / 気づき` の骨格は維持する

## Compatibility Note

- 旧 `team-summary` 的な共有は、main UX ではなく `daily-report` の `共有用` へ役割を移したとみなす
- 旧 `slack` 用途は main UX から外す
- 互換説明は残してよいが、description や sample output の中心には置かない

## Structured Judgment Log

下書き生成時に以下の structured log を `[PostDraft]` タグで出力する。
これによりデバッグ時に「なぜこのトピックが選ばれたか」を追跡可能にする。

```
[PostDraft] judgment: selected_group_id={group_id} | topic_tier={1|2|3} | topic_reason={reason} | reader={auto|override} | reader_reason={reason} | degrade_level={full|limited|empty} | source_count={N}
```

フィールド定義:

- `selected_group_id`: 中心トピックとして選んだ group の ID（例: `group-003`）
- `topic_tier`: 3段フォールバックのどの tier で決まったか
- `topic_reason`: tier 内の選定理由（例: `highest_event_count_with_ai+git`）
- `reader`: `auto`（自動推定）または `override`（ユーザー指定）
- `reader_reason`: reader 推定根拠（例: `default_technical_developer` or `user_specified_非エンジニア`）
- `degrade_level`: `full`（3+ sources）/ `limited`（1-2 sources）/ `empty`（0 sources）
- `source_count`: 成功した source の数

## Escalation Conditions

以下の場合のみ確認を入れてよい:

- **公開向けか私的メモか判断できない場合**: `--reader` 未指定で、入力から reader を抽出できず、かつ公開境界シグナルが判定不能な時は「公開記事向けと私的メモのどちらですか？」と 1 回だけ確認してよい。推定可能なら ask しない
  - 判定不能の例:
    - 公開向けシグナル（`記事`, `ブログ`, `投稿`, `公開`, `共有`）と私的メモシグナル（`自分用`, `個人メモ`, `日記`, `非公開`）が同時に出ていて矛盾する
    - 公開/私的を示すシグナルがどちらも無く、文脈上どちらにも解釈できる
  - この確認が不要な場合は ask せず、デフォルト読者を適用して進む
- それ以外は ask せずに degrade して進む

## Verification Policy

- 主題選定そのものを unit test の pass/fail 条件にはしない
- 理由: `post-draft` の価値は wording と narrative continuity にあり、決定論的 helper に閉じないから
- 検証は fixture ベースの sample review で行う
- 自動テストは `aggregate.py` の shape、mixed-scope 表示、graceful degrade など決定論的な部分だけに限定する

サンプルと fixture review 手順は `references/sample-outputs.md` を参照すること。

## Completion Check

以下を満たすまで出力を確定しない。

- `post-draft` が `Context & Narrative` の skill として一貫して説明されている
- main UX が `0 ask + optional override` になっている
- `team-summary` / `slack` が main UX から外れている
- 主題選定の 3 段フォールバックが `SKILL.md` だけで読める
- 主題選定を Python helper に切り出さないと明記されている
- `reader` の自動推定と `--reader` override の扱いが読める
- workspace 指定が strict repo filter ではないと読める
- mixed-scope 注記ルールが `sources[].scope` ベースで一意に読める
- unit test を書かない理由と fixture review による代替検証が文書化されている

この skill は、その日の一次情報から narrative draft を 1 コマンドで完走させる前提で使う。
