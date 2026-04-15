import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { BookOpen, Plus, Loader2, Play, GitMerge, FileCode2, Target, Search, Database, ShieldAlert, Trash2, X } from 'lucide-react';
import { fetchPlaybooks, createPlaybook, deletePlaybook, type Playbook, getUser } from '../api/client';

export default function Playbooks() {
    const user = getUser();
    const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [showForm, setShowForm] = useState(false);

    // Form state
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [icon, setIcon] = useState('🔍');
    const [taskType, setTaskType] = useState('log_analysis');
    // Audit 4.9: each step carries a stable id so React's reconciliation
    // doesn't reuse the wrong <textarea> DOM node when a step is removed or
    // reordered. `genStepId` is a cheap monotonic fallback for environments
    // without `crypto.randomUUID`.
    type Step = { id: string; text: string };
    const genStepId = () =>
        (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
            ? crypto.randomUUID()
            : `step-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const [steps, setSteps] = useState<Step[]>([{ id: genStepId(), text: '' }]);
    const [override, setOverride] = useState('');
    const [submitting, setSubmitting] = useState(false);

    const loadData = async () => {
        try {
            setLoading(true);
            const data = await fetchPlaybooks();
            setPlaybooks(data);
        } catch (err: any) {
            setError(err.message || 'Failed to load playbooks');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadData();
    }, []);

    const handleDelete = async (id: string) => {
        if (!confirm('Are you sure you want to delete this playbook?')) return;
        try {
            await deletePlaybook(id);
            await loadData();
        } catch (e: any) {
            alert(e.message || 'Failed to delete');
        }
    };

    const handleCreate = async (e: React.FormEvent) => {
        e.preventDefault();
        setSubmitting(true);
        try {
            const validTexts = steps.map(s => s.text).filter(s => s.trim().length > 0);
            if (validTexts.length === 0) throw new Error("At least one step is required");
            if (validTexts.length > 3) throw new Error("Maximum 3 steps allowed");

            await createPlaybook({
                name,
                description,
                icon,
                task_type: taskType,
                system_prompt_override: override || undefined,
                steps: validTexts
            });
            setShowForm(false);
            // Reset form
            setName(''); setDescription(''); setIcon('🔍'); setTaskType('log_analysis');
            setSteps([{ id: genStepId(), text: '' }]); setOverride('');
            await loadData();
        } catch (err: any) {
            alert(err.message || 'Failed to create playbook');
        } finally {
            setSubmitting(false);
        }
    };

    const addStep = () => {
        if (steps.length < 3) setSteps([...steps, { id: genStepId(), text: '' }]);
    };

    const updateStep = (index: number, val: string) => {
        const newSteps = steps.slice();
        newSteps[index] = { ...newSteps[index], text: val };
        setSteps(newSteps);
    };

    const removeStep = (index: number) => {
        if (steps.length > 1) {
            const newSteps = steps.slice();
            newSteps.splice(index, 1);
            setSteps(newSteps);
        }
    };

    const getTaskTypeIcon = (type: string) => {
        switch (type) {
            case 'log_analysis': return <Database className="w-3.5 h-3.5 mr-1" />;
            case 'threat_hunt': return <Target className="w-3.5 h-3.5 mr-1" />;
            case 'incident_response': return <ShieldAlert className="w-3.5 h-3.5 mr-1" />;
            case 'code_audit': return <FileCode2 className="w-3.5 h-3.5 mr-1" />;
            default: return <Search className="w-3.5 h-3.5 mr-1" />;
        }
    };

    const getTaskTypeLabel = (type: string) => {
        return type.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64 text-slate-500">
                <Loader2 className="w-8 h-8 animate-spin" />
                <span className="ml-3 font-medium">Loading playbooks...</span>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-white tracking-tight flex items-center">
                        <BookOpen className="w-6 h-6 mr-3 text-cyan-500" />
                        Playbooks
                    </h1>
                    <p className="text-slate-400 mt-1">Reusable investigation templates and automated response workflows</p>
                </div>
                {user?.role === 'admin' && (
                    <div className="flex items-center space-x-3">
                        <Link
                            to="/playbooks/builder"
                            className="flex items-center px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700/50 rounded-lg transition-colors font-medium"
                        >
                            <GitMerge className="w-4 h-4 mr-2" />
                            Visual Builder
                        </Link>
                        <button
                            onClick={() => setShowForm(true)}
                            className="flex items-center px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg transition-colors font-medium shadow-lg shadow-cyan-900/20"
                        >
                            <Plus className="w-4 h-4 mr-2" />
                            Create Playbook
                        </button>
                    </div>
                )}
            </div>

            {error && (
                <div className="bg-rose-500/10 border border-rose-500/20 text-rose-400 p-4 rounded-xl flex items-center">
                    {error}
                </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {playbooks.map(pb => (
                    <div key={pb.id} className={`bg-[#1E293B] rounded-xl border flex flex-col transition-all hover:bg-slate-800/80 hover:-translate-y-1 hover:shadow-xl ${pb.is_template ? 'border-cyan-500/50 shadow-cyan-900/10' : 'border-slate-700/50'}`}>
                        <div className="p-5 flex-1 flex flex-col">
                            <div className="flex justify-between items-start mb-4">
                                <div className="text-4xl">{pb.icon}</div>
                                <div className="flex flex-col items-end space-y-2">
                                    {pb.is_template ? (
                                        <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                                            Template
                                        </span>
                                    ) : (
                                        <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider bg-slate-500/10 text-slate-400 border border-slate-500/20">
                                            Custom
                                        </span>
                                    )}
                                    <div className="flex space-x-1">
                                        {!pb.is_template && user?.role === 'admin' && (
                                            <>
                                                <button onClick={() => handleDelete(pb.id)} className="p-1 text-slate-500 hover:text-rose-400 transition-colors">
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            </>
                                        )}
                                    </div>
                                </div>
                            </div>

                            <h3 className="text-lg font-bold text-white mb-2">{pb.name}</h3>
                            <p className="text-sm text-slate-400 mb-4 line-clamp-2 flex-1 relative">{pb.description}</p>

                            <div className="flex items-center space-x-2 mt-auto pt-4 border-t border-slate-700/50">
                                <span className="inline-flex items-center px-2 py-1 rounded-md text-[11px] font-semibold bg-slate-800 text-slate-300 border border-slate-700/50">
                                    {getTaskTypeIcon(pb.task_type)}
                                    {getTaskTypeLabel(pb.task_type)}
                                </span>
                                <span className="inline-flex items-center px-2 py-1 rounded-md text-[11px] font-semibold bg-slate-800 text-slate-300 border border-slate-700/50">
                                    <GitMerge className="w-3.5 h-3.5 mr-1" />
                                    {pb.steps.length} {pb.steps.length === 1 ? 'step' : 'steps'}
                                </span>
                            </div>
                        </div>
                        <div className="p-4 border-t border-slate-700/50 bg-slate-800/30 rounded-b-xl flex justify-between items-center">
                            <div className="text-xs text-slate-500 font-mono">
                                {pb.is_template ? 'Global Template' : 'Tenant Playbook'}
                            </div>
                            {user?.role !== 'viewer' && (
                                <Link
                                    to={`/tasks/new?playbook_id=${pb.id}`}
                                    className="flex items-center px-3 py-1.5 bg-cyan-500/10 text-cyan-400 hover:bg-cyan-500/20 border border-cyan-500/20 rounded-md transition-colors text-xs font-bold uppercase tracking-wider"
                                >
                                    <Play className="w-3 h-3 mr-1.5" />
                                    Use Playbook
                                </Link>
                            )}
                        </div>
                    </div>
                ))}
            </div>

            {/* Create Playbook Modal */}
            {showForm && (
                <div className="fixed inset-0 bg-slate-900/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
                    <div className="bg-[#0F172A] border border-slate-700 rounded-2xl w-full max-w-3xl max-h-[90vh] overflow-y-auto shadow-2xl">
                        <div className="sticky top-0 bg-[#0F172A] border-b border-slate-700 p-6 flex justify-between items-center z-10">
                            <h2 className="text-xl font-bold text-white">Create Custom Playbook</h2>
                            <button onClick={() => setShowForm(false)} className="text-slate-400 hover:text-white transition-colors">
                                <X className="w-6 h-6" />
                            </button>
                        </div>

                        <form onSubmit={handleCreate} className="p-6 space-y-6">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <div>
                                    <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Playbook Name</label>
                                    <input
                                        type="text" required
                                        value={name} onChange={e => setName(e.target.value)}
                                        className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500"
                                        placeholder="e.g. AWS Credential Leak Response"
                                    />
                                </div>
                                <div className="grid grid-cols-3 gap-6">
                                    <div className="col-span-1">
                                        <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Icon (Emoji)</label>
                                        <input
                                            type="text" required maxLength={2}
                                            value={icon} onChange={e => setIcon(e.target.value)}
                                            className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 text-center text-xl"
                                        />
                                    </div>
                                    <div className="col-span-2">
                                        <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Task Type</label>
                                        <select
                                            value={taskType} onChange={e => setTaskType(e.target.value)}
                                            className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500"
                                        >
                                            <option value="log_analysis">Log Analysis</option>
                                            <option value="threat_hunt">Threat Hunt</option>
                                            <option value="incident_response">Incident Response</option>
                                            <option value="code_audit">Code Audit</option>
                                            <option value="ioc_scan">IOC Scan</option>
                                        </select>
                                    </div>
                                </div>
                            </div>

                            <div>
                                <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Description</label>
                                <textarea
                                    rows={2} required
                                    value={description} onChange={e => setDescription(e.target.value)}
                                    className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 resize-none"
                                    placeholder="Briefly describe what this playbook accomplishes..."
                                />
                            </div>

                            <div className="border-t border-slate-700 pt-6">
                                <div className="flex justify-between items-center mb-4">
                                    <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider">Investigation Steps</label>
                                    <span className="text-xs text-slate-500 font-medium">{steps.length}/3 steps used</span>
                                </div>

                                <div className="space-y-4">
                                    {steps.map((step, index) => (
                                        <div key={step.id} className="flex space-x-3 items-start relative pb-2">
                                            <div className="shrink-0 w-8 h-8 rounded-full bg-cyan-500/10 text-cyan-400 font-bold flex items-center justify-center border border-cyan-500/20 mt-1">
                                                {index + 1}
                                            </div>
                                            <div className="flex-1">
                                                <textarea
                                                    required rows={2}
                                                    value={step.text}
                                                    onChange={e => updateStep(index, e.target.value)}
                                                    className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 resize-none"
                                                    placeholder={`Instructions for step ${index + 1}...`}
                                                />
                                            </div>
                                            {steps.length > 1 && (
                                                <button type="button" onClick={() => removeStep(index)} className="shrink-0 p-2 mt-2 text-slate-500 hover:text-rose-400 transition-colors">
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>

                                {steps.length < 3 && (
                                    <button
                                        type="button" onClick={addStep}
                                        className="mt-4 flex items-center text-sm font-medium text-cyan-400 hover:text-cyan-300 transition-colors"
                                    >
                                        <Plus className="w-4 h-4 mr-1" /> Add Step
                                    </button>
                                )}
                            </div>

                            <div className="border-t border-slate-700 pt-6">
                                <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">System Prompt Override (Optional)</label>
                                <p className="text-xs text-slate-500 mb-2">Additional context or custom rules appended to the base security prompt for all steps in this playbook.</p>
                                <textarea
                                    rows={3}
                                    value={override} onChange={e => setOverride(e.target.value)}
                                    className="w-full bg-[#1E293B] border border-slate-700 rounded-lg px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 resize-none font-mono"
                                    placeholder="e.g. Always output findings in Markdown tables. Treat all internal IPs in the 10.x.x.x range as highly sensitive."
                                />
                            </div>

                            <div className="border-t border-slate-700 pt-6 flex justify-end space-x-3">
                                <button
                                    type="button" onClick={() => setShowForm(false)}
                                    className="px-5 py-2.5 rounded-lg font-medium text-slate-300 hover:text-white hover:bg-slate-800 transition-colors"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="submit" disabled={submitting}
                                    className="flex items-center px-5 py-2.5 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg transition-colors font-medium shadow-lg shadow-cyan-900/20 disabled:opacity-50"
                                >
                                    {submitting ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
                                    {submitting ? 'Saving...' : 'Save Playbook'}
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
            )}
        </div>
    );
}
