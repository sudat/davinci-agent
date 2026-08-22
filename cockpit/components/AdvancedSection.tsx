/**
 * Advanced intake options — COLLAPSED by default (native <details> without
 * the `open` attribute). Contains: must-include moments, target length
 * range, publication constraints, target audience, presentation intensity.
 * All fields are optional and uncontrolled; nothing here blocks Create.
 */
export default function AdvancedSection() {
  return (
    <details className="advanced" data-testid="advanced-section">
      <summary>高度な設定（任意）</summary>
      <div className="advanced-body">
        <div className="field">
          <label htmlFor="must-include">必須シーン（must-include）</label>
          <textarea
            id="must-include"
            name="must_include"
            placeholder="必ず残したいシーン・発話（1行に1つ）"
          />
          <p className="field-hint">指定しなければ編集側の判断に委ねられます。</p>
        </div>
        <div className="field">
          <label htmlFor="target-length-min">目標尺（分・下限）</label>
          <input
            id="target-length-min"
            name="target_length_min"
            type="number"
            min={0}
            placeholder="例: 8"
          />
        </div>
        <div className="field">
          <label htmlFor="target-length-max">目標尺（分・上限）</label>
          <input
            id="target-length-max"
            name="target_length_max"
            type="number"
            min={0}
            placeholder="例: 12"
          />
        </div>
        <div className="field">
          <label htmlFor="publication-constraints">公開制約</label>
          <input
            id="publication-constraints"
            name="publication_constraints"
            type="text"
            placeholder="例: 平日18時以降に公開 / ショート版も出す"
          />
        </div>
        <div className="field">
          <label htmlFor="target-audience">対象視聴者</label>
          <input
            id="target-audience"
            name="target_audience"
            type="text"
            placeholder="例: 初心者向けの撮影術を見る層"
          />
        </div>
        <div className="field">
          <label htmlFor="presentation-intensity">提示強度</label>
          <select
            id="presentation-intensity"
            name="presentation_intensity"
            defaultValue="standard"
          >
            <option value="low">低め（落ち着いた構成）</option>
            <option value="standard">標準</option>
            <option value="high">高め（強めのテンポ・強調）</option>
          </select>
        </div>
      </div>
    </details>
  );
}
