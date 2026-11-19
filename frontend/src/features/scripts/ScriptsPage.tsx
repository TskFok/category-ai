import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  message,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { CheckCircleOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";
import type { ScriptSegment, ScriptValidation } from "../../shared/types/api";

interface Props {
  novelId: string | null;
  initialEpisodeId?: string | null;
}

const EMOTIONS = [
  "neutral",
  "happy",
  "sad",
  "angry",
  "fearful",
  "calm",
  "tense",
  "whisper",
];

export function ScriptsPage({ novelId, initialEpisodeId }: Props) {
  const qc = useQueryClient();
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(initialEpisodeId || null);
  const [reviewFilter, setReviewFilter] = useState<string | undefined>(undefined);
  const [validation, setValidation] = useState<ScriptValidation | null>(null);

  const plansQuery = useQuery({
    queryKey: ["episode-plans", novelId],
    queryFn: () => api.listEpisodePlans(novelId!),
    enabled: !!novelId,
  });

  const latestPlanId = plansQuery.data?.[0]?.id || null;
  const episodesQuery = useQuery({
    queryKey: ["episodes", latestPlanId],
    queryFn: () => api.listEpisodes(latestPlanId!),
    enabled: !!latestPlanId,
  });

  const activeEpisodeId = selectedEpisodeId || episodesQuery.data?.[0]?.id || null;

  const segmentsQuery = useQuery({
    queryKey: ["script-segments", activeEpisodeId, reviewFilter],
    queryFn: () => api.listEpisodeSegments(activeEpisodeId!, reviewFilter),
    enabled: !!activeEpisodeId,
  });

  const charsQuery = useQuery({
    queryKey: ["characters-for-script", novelId],
    queryFn: () => api.listCharacters(novelId!, { page: 1, pageSize: 100 }),
    enabled: !!novelId,
  });

  const analyzeMut = useMutation({
    mutationFn: () => api.analyzeEpisodeScript(activeEpisodeId!),
    onSuccess: (segs) => {
      message.success(`剧本标注完成，共 ${segs.length} 段（已保留原有稳定 ID 与人工修改）`);
      setValidation(null);
      qc.invalidateQueries({ queryKey: ["script-segments", activeEpisodeId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const validateMut = useMutation({
    mutationFn: () => api.validateEpisodeScript(activeEpisodeId!),
    onSuccess: (res) => {
      setValidation(res);
      if (res.ready_to_render) {
        message.success("剧本校验通过，可直接进入音频合成");
      } else {
        message.warning(
          `发现 ${res.unresolved_segments.length} 条未确认说话人，${res.missing_voices.length} 个缺失音色`
        );
      }
    },
    onError: (e: Error) => message.error(e.message),
  });

  const updateSegMut = useMutation({
    mutationFn: ({
      segmentId,
      payload,
    }: {
      segmentId: string;
      payload: Parameters<typeof api.updateScriptSegment>[1];
    }) => api.updateScriptSegment(segmentId, payload),
    onSuccess: () => {
      message.success("片段已更新（受影响音频已标记过期）");
      qc.invalidateQueries({ queryKey: ["script-segments", activeEpisodeId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  const charOptions = [
    { value: "__narrator__", label: "旁白 (default)" },
    ...(charsQuery.data?.items || []).map((c) => ({
      value: c.id,
      label: c.canonical_name,
    })),
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="逐段配音剧本与说话人复核 (SCRIPT-01)">
        <Space wrap>
          <span>选择剧集：</span>
          <Select
            style={{ width: 280 }}
            placeholder="请先在分集规划生成剧集"
            value={activeEpisodeId || undefined}
            onChange={setSelectedEpisodeId}
            options={(episodesQuery.data || []).map((ep) => ({
              value: ep.id,
              label: `第${ep.episode_no}集：${ep.title}`,
            }))}
          />
          <Select
            allowClear
            style={{ width: 160 }}
            placeholder="按审核状态筛选"
            value={reviewFilter}
            onChange={setReviewFilter}
            options={[
              { value: "needs_review", label: "待人工复核" },
              { value: "auto_accepted", label: "自动通过" },
              { value: "confirmed", label: "已人工确认" },
              { value: "skip", label: "跳过不配音" },
            ]}
          />
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            disabled={!activeEpisodeId}
            loading={analyzeMut.isPending}
            onClick={() => analyzeMut.mutate()}
          >
            DeepSeek 标注配音剧本
          </Button>
          <Button
            icon={<CheckCircleOutlined />}
            disabled={!activeEpisodeId}
            loading={validateMut.isPending}
            onClick={() => validateMut.mutate()}
          >
            渲染前剧本校验
          </Button>
        </Space>
        {validation && (
          <Alert
            style={{ marginTop: 12 }}
            type={validation.ready_to_render ? "success" : "warning"}
            showIcon
            message={
              validation.ready_to_render
                ? `校验通过：${validation.active_segments} 个有效片段均已归属说话人且锁定音色`
                : `渲染受阻：未确认说话人 ${validation.unresolved_segments.length} 段，缺失音色 ${validation.missing_voices.length} 个`
            }
            description={
              validation.unresolved_segments.length > 0
                ? `阻塞段序号：${validation.unresolved_segments.map((u) => `#${u.order_no}`).join(", ")}`
                : undefined
            }
          />
        )}
      </Card>

      <Card title={`剧本片段 (${segmentsQuery.data?.length ?? 0} 段)`}>
        <Table
          rowKey="id"
          loading={segmentsQuery.isLoading}
          dataSource={segmentsQuery.data || []}
          pagination={{ pageSize: 30 }}
          columns={[
            { title: "#", dataIndex: "order_no", width: 55 },
            {
              title: "类型",
              dataIndex: "segment_type",
              width: 110,
              render: (t: string) => <Tag color={t === "dialogue" ? "geekblue" : "default"}>{t}</Tag>,
            },
            {
              title: "原文（忠于原作）",
              dataIndex: "text_content",
              render: (txt: string, r: ScriptSegment) => (
                <div>
                  <Typography.Text>{txt}</Typography.Text>
                  <div style={{ fontSize: 12, color: "#888" }}>
                    偏移 [{r.start_offset}, {r.end_offset}) | 置信度 {(r.confidence * 100).toFixed(0)}%
                    {r.manual_override && <Tag color="gold" style={{ marginLeft: 6 }}>人工锁定</Tag>}
                  </div>
                </div>
              ),
            },
            {
              title: "说话人 / 旁白",
              width: 180,
              render: (_, r: ScriptSegment) => (
                <Select
                  size="small"
                  style={{ width: 160 }}
                  value={r.speaker_character_id || (r.narrator_key ? "__narrator__" : undefined)}
                  placeholder="待指定角色"
                  options={charOptions}
                  onChange={(val) => {
                    if (val === "__narrator__") {
                      updateSegMut.mutate({
                        segmentId: r.id,
                        payload: { set_narrator: true, narrator_key: "default" },
                      });
                    } else {
                      updateSegMut.mutate({
                        segmentId: r.id,
                        payload: { speaker_character_id: val },
                      });
                    }
                  }}
                />
              ),
            },
            {
              title: "情绪",
              width: 120,
              render: (_, r: ScriptSegment) => (
                <Select
                  size="small"
                  style={{ width: 105 }}
                  value={r.emotion}
                  options={EMOTIONS.map((e) => ({ value: e, label: e }))}
                  onChange={(val) =>
                    updateSegMut.mutate({ segmentId: r.id, payload: { emotion: val } })
                  }
                />
              ),
            },
            {
              title: "审核状态",
              dataIndex: "review_status",
              width: 120,
              render: (st: string) => (
                <Tag color={st === "needs_review" ? "red" : st === "confirmed" ? "green" : "blue"}>
                  {st}
                </Tag>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
