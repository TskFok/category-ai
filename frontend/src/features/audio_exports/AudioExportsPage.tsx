import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, message, Select, Space, Statistic, Table, Tag } from "antd";
import { DownloadOutlined, ExportOutlined } from "@ant-design/icons";
import { api, getAuthorizedMediaUrl } from "../../shared/api/client";
import type { AudioExport } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function AudioExportsPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | undefined>(undefined);
  const [format, setFormat] = useState<"mp3" | "wav" | "zip">("mp3");

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

  const readinessQuery = useQuery({
    queryKey: ["audio-readiness", novelId, latestPlanId],
    queryFn: () => api.getNovelAudioReadiness(novelId!, latestPlanId || undefined),
    enabled: !!novelId,
  });

  const exportsQuery = useQuery({
    queryKey: ["audio-exports", novelId],
    queryFn: () => api.listAudioExports(novelId!),
    enabled: !!novelId,
  });

  const exportMut = useMutation({
    mutationFn: () =>
      api.createAudioExport(novelId!, {
        episode_id: format === "zip" ? undefined : selectedEpisodeId,
        plan_id: format === "zip" ? latestPlanId || undefined : undefined,
        format,
      }),
    onSuccess: (exp) => {
      message.success(`音频导出成功 (${(exp.actual_format || exp.format).toUpperCase()})`);
      qc.invalidateQueries({ queryKey: ["audio-exports", novelId] });
      qc.invalidateQueries({ queryKey: ["audio-readiness", novelId, latestPlanId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  const readiness = readinessQuery.data;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {readiness && (
        <Card title="导出前就绪度检查">
          <Space size="large" wrap>
            <Statistic
              title="就绪集数"
              value={`${readiness.ready_episodes} / ${readiness.total_episodes}`}
            />
            <Statistic title="待合成/重算字符" value={readiness.remaining_chars} />
            <Statistic
              title={`预估剩余费用 (${readiness.currency})`}
              value={readiness.estimated_remaining_cost}
              precision={4}
            />
          </Space>
          {!readiness.ready_for_export && (
            <Alert
              style={{ marginTop: 12 }}
              type="warning"
              showIcon
              message="存在未合成、已过期或说话人未确认的片段，导出将被拦截，请先在音频制作页完成渲染。"
            />
          )}
        </Card>
      )}

      <Card title="音频拼接与导出 (EXPORT-01)">
        <Space wrap>
          <span>导出格式：</span>
          <Select
            style={{ width: 160 }}
            value={format}
            onChange={setFormat}
            options={[
              { value: "mp3", label: "单集 MP3" },
              { value: "wav", label: "单集 WAV" },
              { value: "zip", label: "整书 ZIP 打包" },
            ]}
          />
          {format !== "zip" && (
            <Select
              style={{ width: 280 }}
              placeholder="选择要导出的剧集"
              value={selectedEpisodeId}
              onChange={setSelectedEpisodeId}
              options={(episodesQuery.data || []).map((ep) => ({
                value: ep.id,
                label: `第${ep.episode_no}集：${ep.title}`,
              }))}
            />
          )}
          <Button
            type="primary"
            icon={<ExportOutlined />}
            disabled={format !== "zip" && !selectedEpisodeId}
            loading={exportMut.isPending}
            onClick={() => exportMut.mutate()}
          >
            开始拼接并导出
          </Button>
        </Space>
      </Card>

      <Card title="音频导出记录">
        <Table
          rowKey="id"
          loading={exportsQuery.isLoading}
          dataSource={exportsQuery.data || []}
          columns={[
            {
              title: "格式",
              render: (_, r: AudioExport) => (
                <Space>
                  <Tag color="blue">{(r.actual_format || r.format).toUpperCase()}</Tag>
                  {r.is_mock && <Tag color="purple">MOCK</Tag>}
                  {r.is_stale && <Tag color="orange">已过期</Tag>}
                </Space>
              ),
            },
            {
              title: "状态",
              dataIndex: "status",
              render: (st: string) => <Tag color={st === "ready" ? "green" : "orange"}>{st}</Tag>,
            },
            {
              title: "时长",
              dataIndex: "duration_ms",
              render: (ms: number) => `${(ms / 1000).toFixed(1)} 秒`,
            },
            {
              title: "体积",
              dataIndex: "file_size_bytes",
              render: (b: number) => `${(b / 1024).toFixed(1)} KB`,
            },
            {
              title: "校验和",
              dataIndex: "checksum",
              render: (c: string | undefined) => (c ? `${c.slice(0, 12)}...` : "-"),
            },
            { title: "创建时间", dataIndex: "created_at" },
            {
              title: "下载",
              render: (_, r: AudioExport) => (
                <Button
                  size="small"
                  type="primary"
                  icon={<DownloadOutlined />}
                  href={getAuthorizedMediaUrl(r.download_url)}
                  target="_blank"
                >
                  下载音频
                </Button>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
