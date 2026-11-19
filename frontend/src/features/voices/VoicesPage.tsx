import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Input,
  message,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { LockOutlined, PlusOutlined, SoundOutlined } from "@ant-design/icons";
import { api, getAuthorizedMediaUrl } from "../../shared/api/client";
import type { VoiceCandidate, VoiceProfile } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function VoicesPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [targetType, setTargetType] = useState<"narrator" | "character">("narrator");
  const [selectedCharId, setSelectedCharId] = useState<string | undefined>(undefined);
  const [narratorKey, setNarratorKey] = useState("default");
  const [voicePrompt, setVoicePrompt] = useState("");

  const profilesQuery = useQuery({
    queryKey: ["voice-profiles", novelId],
    queryFn: () => api.listVoiceProfiles(novelId!),
    enabled: !!novelId,
  });

  const charsQuery = useQuery({
    queryKey: ["characters-for-voice", novelId],
    queryFn: () => api.listCharacters(novelId!, { page: 1, pageSize: 100 }),
    enabled: !!novelId,
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.createVoiceProfile(novelId!, {
        character_id: targetType === "character" ? selectedCharId : null,
        narrator_key: targetType === "narrator" ? narratorKey : null,
        voice_prompt: voicePrompt,
        consent_confirmed: true,
      }),
    onSuccess: () => {
      message.success("已创建角色声音模板");
      setVoicePrompt("");
      qc.invalidateQueries({ queryKey: ["voice-profiles", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const designMut = useMutation({
    mutationFn: (profileId: string) => api.designVoiceCandidates(profileId),
    onSuccess: () => {
      message.success("已生成候选音色与试听音频");
      qc.invalidateQueries({ queryKey: ["voice-profiles", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const lockMut = useMutation({
    mutationFn: ({ profileId, candidateIndex }: { profileId: string; candidateIndex?: number }) =>
      api.lockVoiceProfile(profileId, {
        candidate_index: candidateIndex,
        scope: "all_episodes",
      }),
    onSuccess: (res) => {
      const cur = res.currency || "CNY";
      message.success(
        `已锁定音色 ${res.profile.voice_id} (v${res.profile.profile_version})：影响 ${res.impacted_segments} 段 / ${res.impacted_chars} 字，已失效 ${res.invalidated_audio_segments ?? 0} 段，预估重算 ${res.estimated_rerender_cost.toFixed(4)} ${cur}`
      );
      qc.invalidateQueries({ queryKey: ["voice-profiles", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const syncStatusMut = useMutation({
    mutationFn: (profileId: string) => api.getVoiceProviderStatus(profileId),
    onSuccess: (res) => {
      message.info(`音色 ${res.voice_id || "-"} 供应商状态：${res.provider_status}`);
      qc.invalidateQueries({ queryKey: ["voice-profiles", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="新建角色配音模板 (VOICE-01)">
        <Space wrap>
          <Select
            style={{ width: 130 }}
            value={targetType}
            onChange={setTargetType}
            options={[
              { value: "narrator", label: "旁白角色" },
              { value: "character", label: "小说人物" },
            ]}
          />
          {targetType === "narrator" ? (
            <Input
              style={{ width: 160 }}
              value={narratorKey}
              onChange={(e) => setNarratorKey(e.target.value)}
              placeholder="旁白标识如 default"
            />
          ) : (
            <Select
              style={{ width: 220 }}
              placeholder="选择人物"
              value={selectedCharId}
              onChange={setSelectedCharId}
              options={(charsQuery.data?.items || []).map((c) => ({
                value: c.id,
                label: `${c.canonical_name} (提及 ${c.mention_count})`,
              }))}
            />
          )}
          <Input
            style={{ width: 320 }}
            value={voicePrompt}
            onChange={(e) => setVoicePrompt(e.target.value)}
            placeholder="声音设计描述（留空则由 DeepSeek 自动推荐）"
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            loading={createMut.isPending}
            disabled={targetType === "character" && !selectedCharId}
            onClick={() => createMut.mutate()}
          >
            创建声线模板
          </Button>
        </Space>
      </Card>

      <Card title="已配置的配音角色与固定音色 (voice_id)">
        <Table
          rowKey="id"
          loading={profilesQuery.isLoading}
          dataSource={profilesQuery.data || []}
          expandable={{
            expandedRowRender: (record: VoiceProfile) => (
              <Space direction="vertical" style={{ width: "100%" }}>
                <Typography.Text strong>候选音色试听与锁定：</Typography.Text>
                {(record.candidates_json || []).length === 0 ? (
                  <Typography.Text type="secondary">
                    尚未生成候选音色，请点击「AI 设计候选音色」。
                  </Typography.Text>
                ) : (
                  (record.candidates_json || []).map((c: VoiceCandidate) => (
                    <Card key={c.index} size="small" title={`${c.label} (${c.voice_id})`}>
                      <Space direction="vertical" style={{ width: "100%" }}>
                        <Typography.Text>{c.voice_prompt}</Typography.Text>
                        <Space>
                          <audio
                            controls
                            src={getAuthorizedMediaUrl(
                              `/api/v1/voice-profiles/${record.id}/preview?candidate_index=${c.index}`
                            )}
                          />
                          <Button
                            type="primary"
                            size="small"
                            icon={<LockOutlined />}
                            loading={lockMut.isPending}
                            onClick={() =>
                              lockMut.mutate({ profileId: record.id, candidateIndex: c.index })
                            }
                          >
                            锁定此音色
                          </Button>
                        </Space>
                      </Space>
                    </Card>
                  ))
                )}
              </Space>
            ),
          }}
          columns={[
            { title: "配音对象", dataIndex: "display_name" },
            {
              title: "类型",
              render: (_, r: VoiceProfile) =>
                r.narrator_key ? <Tag color="purple">旁白:{r.narrator_key}</Tag> : <Tag color="blue">人物</Tag>,
            },
            {
              title: "固定 voice_id",
              dataIndex: "voice_id",
              render: (v: string | null) => v || <Typography.Text type="secondary">未生成</Typography.Text>,
            },
            {
              title: "状态 / 版本",
              render: (_, r: VoiceProfile) => (
                <Space wrap>
                  <Tag color={r.approval_status === "locked" ? "green" : "orange"}>
                    {r.approval_status}
                  </Tag>
                  <Tag>v{r.profile_version}</Tag>
                  {r.provider_status && (
                    <Tag color={r.provider_status === "OK" ? "green" : "gold"}>
                      {r.provider_status}
                    </Tag>
                  )}
                  {r.is_active && <Tag color="cyan">当前激活</Tag>}
                </Space>
              ),
            },
            {
              title: "操作",
              render: (_, r: VoiceProfile) => (
                <Space wrap>
                  <Button
                    size="small"
                    icon={<SoundOutlined />}
                    loading={designMut.isPending}
                    onClick={() => designMut.mutate(r.id)}
                  >
                    AI 设计候选音色
                  </Button>
                  {r.voice_id && (
                    <Button
                      size="small"
                      loading={syncStatusMut.isPending}
                      onClick={() => syncStatusMut.mutate(r.id)}
                    >
                      查状态
                    </Button>
                  )}
                  {r.voice_id && r.approval_status !== "locked" && (
                    <Button
                      size="small"
                      type="primary"
                      icon={<LockOutlined />}
                      onClick={() => lockMut.mutate({ profileId: r.id })}
                    >
                      锁定
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
