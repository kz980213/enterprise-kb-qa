<script setup lang="ts">
import { computed } from 'vue'
import type { Citation } from '@/types'

const props = defineProps<{ citation: Citation }>()

// 分数颜色：> 0.6 绿，> 0.35 橙，否则灰
const scoreColor = computed(() => {
  if (props.citation.score > 0.6) return '#22c55e'
  if (props.citation.score > 0.35) return '#f59e0b'
  return '#94a3b8'
})

// 文件名截断：超过 28 字符显示省略号
const shortName = computed(() => {
  const name = props.citation.source
  return name.length > 28 ? name.slice(0, 25) + '…' : name
})
</script>

<template>
  <div class="citation-card" :title="citation.source">
    <!-- 标记号 -->
    <span class="marker">{{ citation.marker }}</span>

    <!-- 元数据 -->
    <div class="meta">
      <span class="source">{{ shortName }}</span>
      <span class="detail">
        <template v-if="citation.page_number !== null">
          第 {{ citation.page_number }} 页
        </template>
        <template v-if="citation.section_title">
          <span class="sep">·</span>
          § {{ citation.section_title }}
        </template>
      </span>
    </div>

    <!-- 置信度指示器 -->
    <div class="score-bar">
      <div
        class="score-fill"
        :style="{ width: (citation.score * 100).toFixed(0) + '%', background: scoreColor }"
      ></div>
    </div>
    <span class="score-label" :style="{ color: scoreColor }">
      {{ (citation.score * 100).toFixed(0) }}%
    </span>
  </div>
</template>

<style scoped>
.citation-card {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 7px 10px;
  font-size: 12px;
  cursor: default;
  transition: border-color .15s, box-shadow .15s;
  max-width: 340px;
}
.citation-card:hover {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-light);
}
.marker {
  font-weight: 700;
  color: var(--primary);
  font-size: 11px;
  min-width: 22px;
}
.meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.source {
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail { color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sep { margin: 0 4px; }
.score-bar {
  width: 40px;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  flex-shrink: 0;
  overflow: hidden;
}
.score-fill { height: 100%; border-radius: 2px; transition: width .3s; }
.score-label { font-size: 11px; font-weight: 600; min-width: 28px; text-align: right; flex-shrink: 0; }
</style>
