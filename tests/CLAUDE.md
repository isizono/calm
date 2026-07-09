# tests/ 配下の作業ガイド

テストを追加・変更する前に、テスト規約の正本 `docs/spec/test-convention.md` を読むこと。

最重要原則（詳細・例外はすべて正本に従う）:

- 落ちたときに実バグまたは本物の契約違反を示すテストだけを書く
- mock は外部境界（embedding/relay HTTP・環境変数・時刻・subprocess）に限る。内部関数の mock で自己成就するテストを作らない
- 存在・件数・型チェックで止めず、内容まで assert する
- SKILL.md / docs の文言はテストしない。文書テストは「実装から期待値を導出する整合性 lint」と「frontmatter 等の構造 smoke」の2形のみ許可
