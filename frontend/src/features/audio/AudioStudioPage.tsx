import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  message,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import { PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { api, getAuthorizedMediaUrl } from "../../shared/api/client";
import type { ScriptSegment } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function AudioStudioPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);

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
    queryKey: ["script-segments-audio", activeEpisodeId],
    queryFn: () => api.listEpisodeSegments(activeEpisodeId!),
    enabled: !!activeEpisodeId,
  });

  const renderJobsQuery = useQuery({
    queryKey: ["render-jobs", activeEpisodeId],
    queryFn: () => api.listRenderJobs(activeEpisodeId!),
    enabled: !!activeEpisodeId,
  });

  const usageQuery = useQuery({
    queryKey: ["provider-usage", novelId],
    queryFn: () => api.getProviderUsage(novelId!),
    enabled: !!novelId,
  });

  const renderMut = useMutation({
    mutationFn: () => api.createRenderJob(activeEpisodeId!, { run_inline: true }),
    onSuccess: (job) => {
      message.success(`整集合成完成：状态 ${job.status}`);
      qc.invalidateQueries({ queryKey: ["script-segments-audio", activeEpisodeId] });
      qc.invalidateQueries({ queryKey: ["render-jobs", activeEpisodeId] });
      qc.invalidateQueries({ queryKey: ["provider-usage", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const regenMut = useMutation({
    mutationFn: (segmentId: string) => api.regenerateScriptSegment(segmentId),
    onSuccess: () => {
      message.success("单段音频已局部重生成");
      qc.invalidateQueries({ queryKey: ["script-segments-audio", activeEpisodeId] });
      qc.invalidateQueries({ queryKey: ["provider-usage", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  const latestJob = renderJobsQuery.data?.[0];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="音频制作工作台 (AUDIO-01 / TTS-01)">
        <Space wrap size="large">
          <Select
            style={{ width: 280 }}
            placeholder="选择剧集"
            value={activeEpisodeId || undefined}
            onChange={setSelectedEpisodeId}
            options={(episodesQuery.data || []).map((ep) => ({
              value: ep.id,
              label: `第${ep.episode_no}集：${ep.title}`,
            }))}
          />
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            disabled={!activeEpisodeId}
            loading={renderMut.isPending}
            onClick={() => renderMut.mutate()}
          >
            CosyVoice 批量合成整集
          </Button>
          {usageQuery.data && (
            <Space size="large">
              <Statistic title="TTS 累计字符" value={usageQuery.data.total_input_chars} />
              <Statistic
                title="TTS 累计费用(USD)"
                value={usageQuery.data.total_actual_cost}
                precision={4}
              />
            </Space>
          )}
        </Space>
        {latestJob && (
          <div style={{ marginTop: 12 }}>
            <Tag color={latestJob.status === "completed" ? "green" : "orange"}>
              最近任务：{latestJob.status}
            </Tag>
            <Typography.Text type="secondary">
              完成 {latestJob.progress_json?.completed_segments ?? 0}/
              {latestJob.progress_json?.total_segments ?? 0} 段（缓存命中{" "}
              {latestJob.progress_json?.cached_segments ?? 0} 段）
            </Typography.Text>
          </div>
        )}
      </Card>

      <Card title="逐段试听与局部重生成">
        <Table
          rowKey="id"
          loading={segmentsQuery.isLoading}
          dataSource={segmentsQuery.data || []}
          pagination={{ pageSize: 25 }}
          columns={[
            { title: "#", dataIndex: "order_no", width: 55 },
            {
              title: "角色 / 情绪",
              width: 160,
              render: (_, r: ScriptSegment) => (
                <Space direction="vertical" size={2}>
                  <Tag color="blue">{r.speaker_name || "未指定"}</Tag>
                  <Tag>{r.emotion}</Tag>
                </Space>
              ),
            },
            { title: "台词 / 旁白原文", dataIndex: "text_content" },
            {
              title: "音频状态",
              width: 120,
              render: (_, r: ScriptSegment) => {
                const st = r.current_audio?.status || "unrendered";
                const color = st === "ready" ? "green" : st === "stale" ? "orange" : "default";
                return <Tag color={color}>{st}</Tag>;
              },
            },
            {
              title: "试听",
              width: 260,
              render: (_, r: ScriptSegment) =>
                r.current_audio?.stream_url ? (
                  <audio
                    controls
                    style={{ height: 32, width: 240 }}
                    src={getAuthorizedMediaUrl(r.current_audio.stream_url)}
                  />
                ) : (
                  <Typography.Text type="secondary">暂无音频</Typography.Text>
                ),
            },
            {
              title: "操作",
              width: 120,
              render: (_, r: ScriptSegment) => (
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={regenMut.isPending}
                  onClick={() => regenMut.mutate(r.id)}
                >
                  重生成
                </Button>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
