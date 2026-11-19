export interface PaginatedResponse<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export interface Novel {
  id: string;
  title: string;
  author: string | null;
  source_filename: string;
  source_format: string;
  source_hash: string;
  source_version: number;
  encoding: string;
  total_chars: number;
  chapter_count: number;
  chunk_count: number;
  data_revision: number;
  status: string;
  diagnostics_json: {
    encoding?: string;
    total_chars?: number;
    chapter_count?: number;
    missing_chapter_numbers?: number[];
    duplicate_chapter_titles?: string[];
    garbled_ratio?: number;
  };
  created_at: string;
  updated_at: string;
}

export interface ChapterSummary {
  id: string;
  novel_id: string;
  chapter_no: number;
  declared_no: number | null;
  title: string;
  char_count: number;
  text_hash: string;
}

export interface ChunkOut {
  id: string;
  chunk_index: number;
  start_offset: number;
  end_offset: number;
  text_hash: string;
  processing_status: string;
}

export interface ChapterDetail extends ChapterSummary {
  content: string;
  chunks: ChunkOut[];
}

export interface Job {
  id: string;
  novel_id: string;
  job_type: string;
  state: string;
  requested_by: string;
  budget_limit: number;
  spent_cost: number;
  chapter_from: number | null;
  chapter_to: number | null;
  progress_json: {
    total_tasks?: number;
    completed_tasks?: number;
    failed_tasks?: number;
    pending_tasks?: number;
    current_stage?: string;
  };
  error_summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface StageUsage {
  stage: string;
  model: string;
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
}

export interface UsageSummary {
  novel_id: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_estimated_cost: number;
  by_stage: StageUsage[];
}

export interface CharacterSummary {
  id: string;
  novel_id: string;
  canonical_name: string;
  entity_type: string;
  status: string;
  first_appearance_chapter: number | null;
  mention_count: number;
  summary: string | null;
  merged_into_id: string | null;
  revision: number;
}

export interface AliasItem {
  id: string;
  character_id: string;
  alias_text: string;
  alias_type: string;
  valid_from_chapter: number | null;
  valid_to_chapter: number | null;
  review_status: string;
}

export interface CharacterStateItem {
  id: string;
  state_type: string;
  value_json: Record<string, unknown>;
  valid_from: number | null;
  valid_to: number | null;
  review_status: string;
}

export interface FactItem {
  id: string;
  predicate: string;
  value_json: Record<string, unknown>;
  chapter_no: number | null;
  certainty: string;
  review_status: string;
  evidence_ids: string[];
}

export interface CharacterDetail extends CharacterSummary {
  aliases: AliasItem[];
  states: CharacterStateItem[];
  facts: FactItem[];
}

export interface MentionItem {
  id: string;
  chapter_id: string;
  chapter_no: number;
  chapter_title: string;
  surface_text: string;
  start_offset: number;
  end_offset: number;
  context_snippet: string | null;
  resolution_status: string;
  confidence: number | null;
}

export interface EventItem {
  id: string;
  chapter_id: string | null;
  chapter_no: number | null;
  title: string;
  event_type: string;
  description: string | null;
  narrative_order: number;
  story_time_text: string | null;
  certainty: string;
  review_status: string;
  participant_role: string;
  evidence_ids: string[];
}

export interface RelationshipItem {
  id: string;
  source_character_id: string;
  source_character_name: string;
  target_character_id: string;
  target_character_name: string;
  relation_type: string;
  direction: string;
  description: string | null;
  start_chapter: number | null;
  end_chapter: number | null;
  confidence: number | null;
  review_status: string;
  evidence_ids: string[];
}

export interface GraphNode {
  id: string;
  label: string;
  entity_type: string;
  status: string;
  mention_count: number;
  is_center: boolean;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relation_type: string;
  direction: string;
  description: string | null;
  start_chapter: number | null;
  end_chapter: number | null;
  review_status: string;
  evidence_count: number;
}

export interface EgoGraph {
  center_character_id: string;
  depth: number;
  truncated: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface EvidenceSummary {
  id: string;
  chapter_id: string;
  start_offset: number;
  end_offset: number;
  quote_text: string;
  text_hash: string;
  verification_status: string;
  verification_note: string | null;
}

export interface EvidenceDetail extends EvidenceSummary {
  novel_id: string;
  chapter_no: number;
  chapter_title: string;
  utf16_start_offset: number;
  utf16_end_offset: number;
  context_text: string;
  context_utf16_start: number;
  context_utf16_end: number;
}

export interface AnalysisClaim {
  id: string;
  section: string;
  claim_text: string;
  claim_type: "fact" | "inference" | "unknown";
  certainty: string;
  evidence_ids: string[];
}

export interface AnalysisReport {
  id: string;
  character_id: string;
  report_type: string;
  chapter_from: number | null;
  chapter_to: number | null;
  content_json: Record<string, string>;
  generation_version: number;
  review_status: string;
  model_id: string | null;
  prompt_version: string | null;
  created_at: string;
  claims: AnalysisClaim[];
}

export interface ReviewTask {
  id: string;
  novel_id: string;
  review_type: string;
  target_kind: string;
  target_id: string;
  severity: string;
  state: string;
  summary: string;
  payload_json: Record<string, unknown>;
  decision_note: string | null;
  reviewed_by: string | null;
  created_at: string;
}

export interface ExportArtifact {
  id: string;
  novel_id: string;
  format: string;
  status: string;
  data_revision: number;
  file_size_bytes: number;
  download_url: string;
  created_at: string;
}
