"""Public Moon17 reader's 195 icon glyphs, transcribed from the displayed font.

The font itself is not stored. A private-use CSS icon represents one Chinese
character; this dictionary maps its displayed glyph to searchable text.
"""
_ICON_ROWS = (
    "成 阴 唇 淫 洞 根 经 交 叫 媒 媚 女 肉 乳 物 性 穴 血 欲 嘴 虐 童 芝 舔 棍 蜜 兽 液 贪 喉 鸡 巴 奸 屁 慰 熟 殖 潮 感 肛 酥 胸 插 验 龟 蒂 射 精 颜 缩",
    "爆 菊 幼 恋 伦 轮 缝 密 处 肥 操 臀 膜 跨 踩 蹭 洛 鸡 妓 楼 凤 胴 戳 奶 逼 诱 蒲 荡 骚 偷 露 裸 漏 御 嫩 惑 阴 茎 兽 雞 龜 陽 顏 縮 戀 尿 姦 亂 術 輪",
    "遣 縫 處 躪 鴉 鳳 侶 誘 蕩 騷 禦 尻 屌 脫 扒 光 膚 裙 她 撫 美 豐 胸 揉 擦 粉 腿 跨 禁 摸 享 受 褲 襠 內 具 穴 奮 伸 入 貼 緊 股 動 情 擠 身 壓 羞 濕",
    "嬌 顫 劫 裂 演 衣 硬 癢 體 碰 觸 色 溶 器 官 渴 望 粗 摩 擦 挺 呻 吟 豐 絲 襪 肌 捏 搓 柔 軟 隱 秘 毛 少 溝 汁 愛 透 濕 滑 抽 幾 餓 喘",
)
_GLYPHS = [character for row in _ICON_ROWS for character in row.split()]
assert len(_GLYPHS) == 195 and all(len(character) == 1 for character in _GLYPHS)
ICON_GLYPHS = {index: char for index, char in enumerate(_GLYPHS, 1)}
