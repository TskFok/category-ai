import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Space, Table, Tag, message } from "antd";
import { DownloadOutlined, ExportOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";
import type { ExportArtifact } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function ExportsPage({ novelId }: Props) {
  const qc = useQueryClient();

  const exportsQuery = useQuery({
    queryKey: ["exports", novelId],
    queryFn: () => api.listExports(novelId!),
    enabled: !!novelId,
  });

  const createMut = useMutation({
    mutationFn: (fmt: "json" | "csv" | "graphml") => api.createExport(novelId!, fmt),
    onSuccess: (art) => {
      message.success(`导出成功 (${art.format.toUpperCase()} · rev.${art.data_revision})`);
      qc.invalidateQueries({ queryKey: ["exports", novelId] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  return (
    <Card
      title={
        <Space>
          <ExportOutlined />
          <span>版本化数据导出</span>
        </Space>
      }
      extra={
        <Space>
          <Button
            type="primary"
            loading={createMut.isPending}
            onClick={() => createMut.mutate("json")}
          >
            导出 JSON
          </Button>
          <Button loading={createMut.isPending} onClick={() => createMut.mutate("csv")}>
            导出 CSV
          </Button>
          <Button loading={createMut.isPending} onClick={() => createMut.mutate("graphml")}>
            导出 GraphML
          </Button>
        </Space>
      }
    >
      <Table
        rowKey="id"
        loading={exportsQuery.isLoading}
        dataSource={exportsQuery.data || []}
        columns={[
          {
            title: "格式",
            dataIndex: "format",
            render: (f: string) => <Tag color="blue">{f.toUpperCase()}</Tag>,
          },
          {
            title: "数据快照版本",
            dataIndex: "data_revision",
            render: (v: number) => <Tag>rev.{v}</Tag>,
          },
          {
            title: "文件大小",
            dataIndex: "file_size_bytes",
            render: (b: number) => `${(b / 1024).toFixed(1)} KB`,
          },
          { title: "生成时间", dataIndex: "created_at" },
          {
            title: "下载",
            render: (_, rec: ExportArtifact) => (
              <Button
                size="small"
                type="link"
                icon={<DownloadOutlined />}
                href={rec.download_url}
                target="_blank"
              >
                下载文件
              </Button>
            ),
          },
        ]}
      />
    </Card>
  );
}
