# cc-memory 不変条件一覧

cc-memoryの実装が前提とするデータ不変条件を列挙する。一次情報はコードであり、本ドキュメントと食い違った場合はコードが正である。

各項目には検証根拠（`ファイル:行` またはmigrationファイル名）を付す。

---

## タグ namespace

- タグの namespace は `VALID_NAMESPACES`（`src/services/tag_service.py`）に列挙された値のいずれかである: `''`（素タグ）, `domain`, `intent`, `glossary`, `layer`
- DB側にCHECK制約は無く、検証はPython層のみで行う（`migrations/0039_extend_tag_namespace.sql` でDB側のCHECK制約は撤去済み）。namespace追加にmigrationは不要
- `layer` namespaceの値は当面 `direction` のみ運用する（`layer:direction`）。他の値（例: `layer:precedent`）は未定義。判例＝direction以外の全decisionであり、デフォルト側にタグは不要（タグは例外側にだけ付ける）
- `layer:direction` は decision に直付けされたときのみ方向性decisionとして扱われる（`decision_tags` への直接紐付けが判定条件。topic経由の継承タグでは`direction_service.get_direction_decisions`の対象にならない。継承はdomain絞り込みの条件としてのみ使われる）

---

## 方向性decision（layer:direction）

- 方向性decisionはtitleが必須（`decision_service.add_decisions`のitemバリデーション。省略・空文字はエラー）
- 方向性decisionのハードキャップ（挿入拒否）は無い。件数超過は`direction_overflow` hintでの注意喚起に留める（判定不能を事前goに倒す原則。人間裁定の記録が機械にブロックされる事故を避けるため）
