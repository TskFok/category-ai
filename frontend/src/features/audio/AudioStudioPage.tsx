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
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  PauseCircleOutlined,
  PlayCircleOutlined,
  RedoOutlined,
  ReloadOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { api, getAuthorizedMediaUrl } from "../../shared/api/client";
import type { ScriptSegment } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function AudioStudioPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [activeBookJobId, setActiveBookJobId] = useState<string | null>(null);

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
    refetchInterval: (query) => {
      const latest = query.state.data?.[0];
      return latest && (latest.status === "pending" || latest.status === "running") ? 2000 : false;
    },
  });

  const bookJobQuery = useQuery({
    queryKey: ["book-render-job", activeBookJobId],
    queryFn: () => api.getBookRenderJob(activeBookJobId!),
    enabled: !!activeBookJobId,
    refetchInterval: (query) => {
      const st = query.state.data?.status;
      return st && (st === "pending" || st === "running") ? 2500 : false;
    },
  });

  const usageQuery = useQuery({
    queryKey: ["provider-usage", novelId],
    queryFn: () => api.getProviderUsage(novelId!),
    enabled: !!novelId,
  });

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["script-segments-audio", activeEpisodeId] });
    qc.invalidateQueries({ queryKey: ["render-jobs", activeEpisodeId] });
    qc.invalidateQueries({ queryKey: ["provider-usage", novelId] });
  };

  // P1-02: Default async submission so browser never blocks for entire episode
  const renderMut = useMutation({
    mutationFn: () => api.createRenderJob(activeEpisodeId!, { run_inline: false }),
    onSuccess: (job) => {
      message.success(`已提交后台合成任务：${job.status}`);
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const bookRenderMut = useMutation({
    mutationFn: () => api.createBookRenderJob(latestPlanId!, { run_inline: false }),
    onSuccess: (bj) => {
      setActiveBookJobId(bj.id);
      message.success(`已提交整书批量配音任务（共 ${bj.episode_ids.length} 集）`);
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const pauseMut = useMutation({
    mutationFn: (jobId: string) => api.pauseRenderJob(jobId),
    onSuccess: () => {
      message.info("已请求暂停合成任务");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const resumeMut = useMutation({
    mutationFn: (jobId: string) => api.resumeRenderJob(jobId),
    onSuccess: () => {
      message.success("已恢复合成任务");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const cancelMut = useMutation({
    mutationFn: (jobId: string) => api.cancelRenderJob(jobId),
    onSuccess: () => {
      message.warning("已取消合成任务");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const retryFailedMut = useMutation({
    mutationFn: (jobId: string) => api.retryFailedRenderJob(jobId),
    onSuccess: () => {
      message.success("已仅针对失败单元发起重试");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const regenMut = useMutation({
    mutationFn: (segmentId: string) => api.regenerateScriptSegment(segmentId),
    onSuccess: () => {
      message.success("单段音频已局部重生成");
      refreshAll();
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  const latestJob = renderJobsQuery.data?.[0];
  const currency = usageQuery.data?.currency || "CNY";

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="音频制作工作台 (AUDIO-01 / TTS-01 / M4)">
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
            后台合成当前集
          </Button>
          <Button
            icon={<ThunderboltOutlined />}
            disabled={!latestPlanId}
            loading={bookRenderMut.isPending}
            onClick={() => bookRenderMut.mutate()}
          >
            整书批量排队配音
          </Button>
          {usageQuery.data && (
            <Space size="large">
              <Statistic title="TTS 累计字符" value={usageQuery.data.total_input_chars} />
              <Statistic
                title={`TTS 累计费用 (${currency})`}
                value={usageQuery.data.total_actual_cost}
                precision={4}
              />
            </Space>
          )}
        </Space>

        {latestJob && (
          <div style={{ marginTop: 14 }}>
            <Space wrap>
              <Tag color={latestJob.status === "completed" ? "green" : latestJob.status === "failed" ? "red" : "orange"}>
                最近任务：{latestJob.status}
              </Tag>
              <Typography.Text type="secondary">
                完成 {latestJob.progress_json?.completed_segments ?? 0}/
                {latestJob.progress_json?.total_segments ?? 0} 段（缓存命中{" "}
                {latestJob.progress_json?.cached_segments ?? 0}，失败{" "}
                {latestJob.progress_json?.failed_segments ?? 0}）
              </Typography.Text>
              {(latestJob.status === "running" || latestJob.status === "pending") && (
                <>
                  <Button
                    size="small"
                    icon={<PauseCircleOutlined />}
                    onClick={() => pauseMut.mutate(latestJob.id)}
                  >
                    暂停
                  </Button>
                  <Button
                    size="small"
                    danger
                    icon={<StopOutlined />}
                    onClick={() => cancelMut.mutate(latestJob.id)}
                  >
                    取消
                  </Button>
                </>
              )}
              {(latestJob.status === "paused" ||
                latestJob.status === "partial_failed" ||
                latestJob.status === "failed") && (
                <>
                  <Button
                    size="small"
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    onClick={() => resumeMut.mutate(latestJob.id)}
                  >
                    恢复
                  </Button>
                  <Button
                    size="small"
                    icon={<RedoOutlined />}
                    onClick={() => retryFailedMut.mutate(latestJob.id)}
                  >
                    仅重试失败段
                  </Button>
                </>
              )}
            </Space>
            {latestJob.error_summary && (
              <Alert
                style={{ marginTop: 8 }}
                type="warning"
                showIcon
                message={latestJob.error_summary}
              />
            )}
          </div>
        )}

        {bookJobQuery.data && (
          <Alert
            style={{ marginTop: 12 }}
            type={bookJobQuery.data.status === "completed" ? "success" : "info"}
            showIcon
            message={`整书批量任务状态：${bookJobQuery.data.status}（已完成 ${bookJobQuery.data.progress_json?.completed_episodes ?? 0}/${bookJobQuery.data.progress_json?.total_episodes ?? 0} 集，费用 ${bookJobQuery.data.spent_cost.toFixed(4)} ${bookJobQuery.data.currency}）`}
          />
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
              width: 170,
              render: (_, r: ScriptSegment) => (
                <Space direction="vertical" size={2}>
                  <Tag color={r.is_render_blocked ? "red" : "blue"}>
                    {r.speaker_name || "未指定（阻塞）"}
                  </Tag>
                  <Tag>{r.emotion}</Tag>
                </Space>
              ),
            },
            { title: "台词 / 旁白原文", dataIndex: "text_content" },
            {
              title: "音频状态",
              width: 140,
              render: (_, r: ScriptSegment) => {
                const st = r.current_audio?.status || "unrendered";
                const color = st === "ready" ? "green" : st === "stale" ? "orange" : "default";
                return (
                  <Space direction="vertical" size={2}>
                    <Tag color={color}>{st}</Tag>
                    {r.current_audio?.is_mock && <Tag color="purple">MOCK</Tag>}
                  </Space>
                );
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
