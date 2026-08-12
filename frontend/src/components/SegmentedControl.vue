<script setup lang="ts">
export interface SegmentedOption {
  value: string | number;
  label: string;
}

defineProps<{
  options: SegmentedOption[];
  modelValue: string | number;
}>();

const emit = defineEmits<{ "update:modelValue": [value: string | number] }>();
</script>

<template>
  <div class="segmented" role="tablist">
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      role="tab"
      :aria-selected="option.value === modelValue"
      :class="{ active: option.value === modelValue }"
      @click="emit('update:modelValue', option.value)"
    >{{ option.label }}</button>
  </div>
</template>
