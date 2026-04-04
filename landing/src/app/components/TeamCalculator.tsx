import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Legend } from "recharts";

type FormValues = {
  hospitalType: "safety-net" | "rural" | "standard";
  track: "track1" | "track3";
  procedure: "lejr" | "spinal" | "shfft" | "cabg" | "bowel";
  monthlyEpisodes: number;
  readmissionRate: number;
  promCompletion: number;
  failureToRescueRate: number;
  op46Score: number;
};

const TARGET_PRICE: Record<FormValues["procedure"], number> = {
  lejr: 20000,
  spinal: 22000,
  shfft: 25000,
  cabg: 45000,
  bowel: 20000,
};

const CALENDLY_URL = "https://calendly.com/tejxpatel23/archangel-health-intro";

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

function money(v: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(v);
}

function pct(v: number): string {
  return `${v.toFixed(1)}%`;
}

function scoreLowerIsBetter(value: number, min: number, max: number): number {
  const normalized = (max - value) / (max - min);
  return clamp(normalized * 100, 0, 100);
}

function cqsAdjustmentPctFromScore(score: number): number {
  const s = clamp(score, 0, 100);
  if (s <= 40) return -10 + (s / 40) * 5;
  if (s <= 60) return -5 + ((s - 40) / 20) * 5;
  if (s <= 80) return ((s - 60) / 20) * 5;
  return 5 + ((s - 80) / 20) * 5;
}

function hospitalBaselineScores(hospitalType: FormValues["hospitalType"]) {
  if (hospitalType === "safety-net") return { psi90: 50, falls: 52, respFailure: 51 };
  if (hospitalType === "rural") return { psi90: 53, falls: 55, respFailure: 54 };
  return { psi90: 55, falls: 58, respFailure: 57 };
}

function useAnimatedNumber(value: number, duration = 500): number {
  const [display, setDisplay] = useState(value);

  useEffect(() => {
    let raf = 0;
    const start = performance.now();
    const from = display;
    const delta = value - from;
    const step = (ts: number) => {
      const t = clamp((ts - start) / duration, 0, 1);
      const ease = 1 - Math.pow(1 - t, 3);
      setDisplay(from + delta * ease);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps

  return display;
}

export default function TeamCalculator() {
  const { register, watch, setValue } = useForm<FormValues>({
    defaultValues: {
      hospitalType: "standard",
      track: "track1",
      procedure: "spinal",
      monthlyEpisodes: 75,
      readmissionRate: 10,
      promCompletion: 45,
      failureToRescueRate: 18,
      op46Score: 50,
    },
  });

  const values = watch();
  const procedure = values.procedure || "spinal";

  useEffect(() => {
    const defaultRate = procedure === "spinal" ? 10 : 15;
    if (values.readmissionRate === 10 || values.readmissionRate === 15) {
      setValue("readmissionRate", defaultRate);
    }
  }, [procedure]); // eslint-disable-line react-hooks/exhaustive-deps

  const model = useMemo(() => {
    const annualEpisodes = clamp(Number(values.monthlyEpisodes || 10), 10, 500) * 12;
    const targetPrice = TARGET_PRICE[procedure];
    const totalBudget = annualEpisodes * targetPrice;
    const readmissionRatePct = clamp(Number(values.readmissionRate || 0), 1, 40);
    const promCompletionPct = clamp(Number(values.promCompletion || 0), 10, 100);
    const ftrRate = clamp(Number(values.failureToRescueRate || 0), 5, 30);
    const op46 = clamp(Number(values.op46Score || 0), 0, 100);
    const readmissionCostDrag = annualEpisodes * (readmissionRatePct / 100) * 10000;

    const baselines = hospitalBaselineScores(values.hospitalType || "standard");
    const scoresCurrent = {
      readmission: scoreLowerIsBetter(readmissionRatePct, 1, 40),
      psi90: baselines.psi90,
      prom: promCompletionPct,
      falls: baselines.falls,
      respFailure: baselines.respFailure,
      failureToRescue: scoreLowerIsBetter(ftrRate, 5, 30),
      op46,
    };

    const cqs2026 = (scoresCurrent.readmission + scoresCurrent.psi90 + scoresCurrent.prom) / 3;
    const cqs2027 = (
      scoresCurrent.readmission +
      scoresCurrent.psi90 +
      scoresCurrent.prom +
      scoresCurrent.falls +
      scoresCurrent.respFailure +
      scoresCurrent.failureToRescue
    ) / 6;
    const cqs2028 = (
      scoresCurrent.readmission +
      scoresCurrent.psi90 +
      scoresCurrent.prom +
      scoresCurrent.falls +
      scoresCurrent.respFailure +
      scoresCurrent.failureToRescue +
      scoresCurrent.op46
    ) / 7;
    const currentCqs = (cqs2026 + cqs2027 + cqs2028) / 3;

    let currentCqsAdjustmentPct = cqsAdjustmentPctFromScore(currentCqs);
    if (values.track === "track1") currentCqsAdjustmentPct = Math.max(currentCqsAdjustmentPct, 0);
    const currentCqsAdjustmentDollar = totalBudget * (currentCqsAdjustmentPct / 100);
    const currentNetPosition = totalBudget - readmissionCostDrag + currentCqsAdjustmentDollar;

    const improvedReadmissionRate = clamp(readmissionRatePct * 0.69, 1, 40);
    const improvedPromCompletion = Math.max(promCompletionPct, 78);
    const improvedFtrRate = clamp(ftrRate * 0.92, 5, 30); // Indirect effect; conservative.
    const improvedOp46 = Math.max(op46, 80);
    const withArchangelReadmissionDrag = annualEpisodes * (improvedReadmissionRate / 100) * 10000;
    const scoresProjected = {
      ...scoresCurrent,
      readmission: scoreLowerIsBetter(improvedReadmissionRate, 1, 40),
      prom: improvedPromCompletion,
      failureToRescue: scoreLowerIsBetter(improvedFtrRate, 5, 30),
      op46: improvedOp46,
    };

    const projected2026 = (scoresProjected.readmission + scoresProjected.psi90 + scoresProjected.prom) / 3;
    const projected2027 = (
      scoresProjected.readmission +
      scoresProjected.psi90 +
      scoresProjected.prom +
      scoresProjected.falls +
      scoresProjected.respFailure +
      scoresProjected.failureToRescue
    ) / 6;
    const projected2028 = (
      scoresProjected.readmission +
      scoresProjected.psi90 +
      scoresProjected.prom +
      scoresProjected.falls +
      scoresProjected.respFailure +
      scoresProjected.failureToRescue +
      scoresProjected.op46
    ) / 7;
    const projectedCqs = (projected2026 + projected2027 + projected2028) / 3;

    let projectedCqsAdjustmentPct = cqsAdjustmentPctFromScore(projectedCqs);
    if (values.track === "track1") projectedCqsAdjustmentPct = Math.max(projectedCqsAdjustmentPct, 0);
    const projectedCqsAdjustmentDollar = totalBudget * (projectedCqsAdjustmentPct / 100);
    const archangelAnnualCost = annualEpisodes * 300;
    const withArchangelNetPosition = totalBudget - withArchangelReadmissionDrag + projectedCqsAdjustmentDollar - archangelAnnualCost;

    const netSavings = withArchangelNetPosition - currentNetPosition;
    const roi = archangelAnnualCost > 0 ? netSavings / archangelAnnualCost : 0;

    return {
      annualEpisodes,
      targetPrice,
      totalBudget,
      readmissionRatePct,
      promCompletionPct,
      ftrRate,
      op46,
      currentCqs,
      currentCqsAdjustmentPct,
      currentCqsAdjustmentDollar,
      readmissionCostDrag,
      currentNetPosition,
      improvedReadmissionRate,
      improvedPromCompletion,
      improvedFtrRate,
      improvedOp46,
      projectedCqs,
      projectedCqsAdjustmentPct,
      projectedCqsAdjustmentDollar,
      withArchangelReadmissionDrag,
      withArchangelNetPosition,
      archangelAnnualCost,
      readmissionScoreDelta: scoresProjected.readmission - scoresCurrent.readmission,
      promDelta: scoresProjected.prom - scoresCurrent.prom,
      ftrScoreDelta: scoresProjected.failureToRescue - scoresCurrent.failureToRescue,
      op46Delta: scoresProjected.op46 - scoresCurrent.op46,
      netSavings,
      roi,
    };
  }, [values, procedure]);

  const animatedSavings = useAnimatedNumber(model.netSavings, 550);
  const animatedCurrentNet = useAnimatedNumber(model.currentNetPosition, 500);
  const animatedWithArchangelNet = useAnimatedNumber(model.withArchangelNetPosition, 500);
  const animatedRoi = useAnimatedNumber(model.roi, 500);

  const chartData = [
    {
      category: "Readmission cost drag",
      current: Math.round(model.readmissionCostDrag),
      withArchangel: Math.round(model.withArchangelReadmissionDrag),
    },
    {
      category: "CQS adjustment",
      current: Math.round(model.currentCqsAdjustmentDollar),
      withArchangel: Math.round(model.projectedCqsAdjustmentDollar),
    },
    {
      category: "Net TEAM position",
      current: Math.round(model.currentNetPosition),
      withArchangel: Math.round(model.withArchangelNetPosition),
    },
  ];

  return (
    <div className="min-h-screen bg-[#0a0a0b] text-[#f5f5f7] px-4 py-10 md:px-8 md:py-14">
      <div className="mx-auto max-w-7xl space-y-8">
        <header className="space-y-3">
          <h1 className="text-4xl md:text-5xl font-semibold tracking-[-0.02em]">Calculate Your TEAM Financial Exposure</h1>
          <p className="text-base md:text-lg text-[#bfc2d1] max-w-3xl">See what TEAM costs you today - and what Archangel helps you recover.</p>
        </header>

        <section className="grid gap-5 xl:grid-cols-[1.05fr_1fr]">
          <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 md:p-6 space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold">Hospital Profile</h2>
              <p className="text-sm text-[#9ca3b8]">Set your episode mix and current baseline performance.</p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-2 text-sm">
                <span className="text-[#d6d9e8]">Hospital type</span>
                <select className="w-full rounded-xl border border-white/15 bg-[#0b0d12] px-3 py-2.5 outline-none focus:border-[#00ffff]" {...register("hospitalType")}>
                  <option value="standard">Standard IPPS</option>
                  <option value="safety-net">Safety-Net</option>
                  <option value="rural">Rural</option>
                </select>
              </label>

              <label className="space-y-2 text-sm">
                <span className="text-[#d6d9e8]">Primary procedure</span>
                <select className="w-full rounded-xl border border-white/15 bg-[#0b0d12] px-3 py-2.5 outline-none focus:border-[#00ffff]" {...register("procedure")}>
                  <option value="lejr">LEJR</option>
                  <option value="spinal">Spinal Fusion</option>
                  <option value="shfft">SHFFT</option>
                  <option value="cabg">CABG</option>
                  <option value="bowel">Major Bowel</option>
                </select>
              </label>
            </div>

            <div className="space-y-2 text-sm">
              <div className="text-[#d6d9e8]">Participation track</div>
              <div className="grid grid-cols-2 gap-2">
                <label className="rounded-xl border border-white/15 px-3 py-2.5 bg-[#0b0d12] cursor-pointer">
                  <input type="radio" value="track1" className="mr-2 accent-cyan-300" {...register("track")} />
                  Track 1 (upside only)
                </label>
                <label className="rounded-xl border border-white/15 px-3 py-2.5 bg-[#0b0d12] cursor-pointer">
                  <input type="radio" value="track3" className="mr-2 accent-cyan-300" {...register("track")} />
                  Track 3 (+/-20%)
                </label>
              </div>
            </div>

            <label className="space-y-2 text-sm block">
              <span className="text-[#d6d9e8]">Monthly TEAM-eligible episodes</span>
              <input
                type="number"
                min={10}
                max={500}
                className="w-full rounded-xl border border-white/15 bg-[#0b0d12] px-3 py-2.5 outline-none focus:border-[#00ffff]"
                {...register("monthlyEpisodes", { valueAsNumber: true })}
              />
              <div className="text-xs text-[#8b93ad]">Range 10-500 episodes per month</div>
            </label>

            <div className="space-y-4 pt-2">
              <h3 className="text-base font-semibold">Current Performance Metrics</h3>

              <label className="space-y-2 block">
                <div className="flex items-center justify-between text-sm">
                  <span>30-day readmission rate</span>
                  <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs">{pct(model.readmissionRatePct)}</span>
                </div>
                <input type="range" min={1} max={40} className="w-full accent-cyan-300" {...register("readmissionRate", { valueAsNumber: true })} />
              </label>

              <label className="space-y-2 block">
                <div className="flex items-center justify-between text-sm">
                  <span>HOOS/KOOS PROM completion</span>
                  <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs">{pct(model.promCompletionPct)}</span>
                </div>
                <input type="range" min={10} max={100} className="w-full accent-cyan-300" {...register("promCompletion", { valueAsNumber: true })} />
              </label>

              <label className="space-y-2 block">
                <div className="flex items-center justify-between text-sm">
                  <span>Failure to rescue rate (per 100)</span>
                  <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs">{model.ftrRate.toFixed(1)}</span>
                </div>
                <input type="range" min={5} max={30} step={0.1} className="w-full accent-cyan-300" {...register("failureToRescueRate", { valueAsNumber: true })} />
              </label>

              <label className="space-y-2 block">
                <div className="flex items-center justify-between text-sm">
                  <span>OP-46 / Information Transfer</span>
                  <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs">{pct(model.op46)}</span>
                </div>
                <input type="range" min={0} max={100} className="w-full accent-cyan-300" {...register("op46Score", { valueAsNumber: true })} />
              </label>
            </div>
          </div>

          <div className="space-y-5">
            <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 md:p-6">
              <div className="text-sm text-[#9ca3b8]">Net Savings vs. Without Archangel</div>
              <div className={`mt-2 text-4xl md:text-5xl font-semibold tracking-[-0.03em] ${animatedSavings >= 0 ? "text-[#00ffff]" : "text-[#fb7185]"}`}>
                {money(animatedSavings)}
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-xs text-[#9ca3b8]">
                <span className="rounded-full border border-white/15 px-2 py-1">Annual episodes: {model.annualEpisodes}</span>
                <span className="rounded-full border border-white/15 px-2 py-1">Target/episode: {money(model.targetPrice)}</span>
                <span className="rounded-full border border-white/15 px-2 py-1">Total TEAM budget: {money(model.totalBudget)}</span>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <article className="rounded-2xl border border-white/10 bg-[#11131a] p-5 space-y-2">
                <h3 className="font-semibold">Your Current TEAM Position</h3>
                <div className="text-sm text-[#9ca3b8]">Annual Episodes</div>
                <div className="text-lg font-semibold">{model.annualEpisodes}</div>
                <div className="text-sm text-[#9ca3b8]">Total TEAM Budget</div>
                <div className="font-medium">{money(model.totalBudget)}</div>
                <div className="text-sm text-[#9ca3b8]">Readmission Cost Drag</div>
                <div className="font-medium text-[#fda4af]">- {money(model.readmissionCostDrag)}</div>
                <div className="text-sm text-[#9ca3b8]">CQS Adjustment ({pct(model.currentCqsAdjustmentPct)})</div>
                <div className={`font-medium ${model.currentCqsAdjustmentDollar >= 0 ? "text-emerald-300" : "text-[#fda4af]"}`}>
                  {model.currentCqsAdjustmentDollar >= 0 ? "+" : "-"} {money(Math.abs(model.currentCqsAdjustmentDollar))}
                </div>
                <div className="pt-2 border-t border-white/10">
                  <div className="text-sm text-[#9ca3b8]">Net Annual TEAM Position</div>
                  <div className={`text-2xl font-semibold ${animatedCurrentNet >= 0 ? "text-emerald-300" : "text-[#fda4af]"}`}>
                    {money(animatedCurrentNet)}
                  </div>
                </div>
              </article>

              <article className="rounded-2xl border border-cyan-300/30 bg-[#07141a] p-5 space-y-2">
                <h3 className="font-semibold">With Archangel Health</h3>
                <div className="text-sm text-[#9ad9df]">Improved readmission rate</div>
                <div className="font-medium">{pct(model.improvedReadmissionRate)}</div>
                <div className="text-sm text-[#9ad9df]">Annual Archangel cost</div>
                <div className="font-medium">- {money(model.archangelAnnualCost)}</div>
                <div className="text-sm text-[#9ad9df]">New CQS Adjustment ({pct(model.projectedCqsAdjustmentPct)})</div>
                <div className={`font-medium ${model.projectedCqsAdjustmentDollar >= 0 ? "text-[#00ffff]" : "text-[#fda4af]"}`}>
                  {model.projectedCqsAdjustmentDollar >= 0 ? "+" : "-"} {money(Math.abs(model.projectedCqsAdjustmentDollar))}
                </div>
                <div className="pt-2 border-t border-cyan-300/20">
                  <div className="text-sm text-[#9ad9df]">Net TEAM Position with Archangel</div>
                  <div className={`text-2xl font-semibold ${animatedWithArchangelNet >= 0 ? "text-[#00ffff]" : "text-[#fda4af]"}`}>
                    {money(animatedWithArchangelNet)}
                  </div>
                </div>
              </article>
            </div>

            <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
              <div className="text-sm font-medium mb-2">Current vs With Archangel</div>
              <div className="h-[300px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} barCategoryGap={20}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#273042" />
                    <XAxis dataKey="category" tick={{ fill: "#c7cfde", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#c7cfde", fontSize: 12 }} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                    <Tooltip
                      formatter={(value: number) => money(Number(value || 0))}
                      contentStyle={{ background: "#0f1118", border: "1px solid #374151", borderRadius: "10px" }}
                    />
                    <Legend wrapperStyle={{ color: "#d1d5db" }} />
                    <Bar dataKey="current" fill="#6b7280" name="Current" radius={[6, 6, 0, 0]} />
                    <Bar dataKey="withArchangel" fill="#00ffff" name="With Archangel" radius={[6, 6, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </section>

        <section className="rounded-2xl border border-white/10 bg-white/[0.02] p-5 md:p-6 space-y-4">
          <h3 className="text-base font-semibold">Improvement assumptions used</h3>
          <div className="grid gap-3 md:grid-cols-2 text-sm">
            <div className="rounded-xl border border-white/10 p-3">Readmission: <strong>{pct(model.readmissionRatePct)} to {pct(model.improvedReadmissionRate)}</strong> (31% relative reduction)</div>
            <div className="rounded-xl border border-white/10 p-3">PROM completion: <strong>{pct(model.promCompletionPct)} to {pct(model.improvedPromCompletion)}</strong> (+ outreach analog)</div>
            <div className="rounded-xl border border-white/10 p-3">Failure to Rescue: <strong>{model.ftrRate.toFixed(1)} to {model.improvedFtrRate.toFixed(1)} per 100</strong> (indirect, conservative)</div>
            <div className="rounded-xl border border-white/10 p-3">OP-46 score: <strong>{pct(model.op46)} to {pct(model.improvedOp46)}</strong> (education/comprehension analog)</div>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 text-sm text-[#9ca3b8]">
            <div>Readmission component delta: {model.readmissionScoreDelta >= 0 ? "+" : ""}{model.readmissionScoreDelta.toFixed(1)} pts</div>
            <div>PROM component delta: {model.promDelta >= 0 ? "+" : ""}{model.promDelta.toFixed(1)} pts</div>
            <div>FTR component delta*: {model.ftrScoreDelta >= 0 ? "+" : ""}{model.ftrScoreDelta.toFixed(1)} pts</div>
            <div>OP-46 component delta: {model.op46Delta >= 0 ? "+" : ""}{model.op46Delta.toFixed(1)} pts</div>
          </div>
          <p className="text-xs text-[#8f96af]">All projections are based on published research from analogous interventions - not Archangel-specific pilot data. Actual results will vary. This tool is for illustrative purposes only.</p>
        </section>

        <section className="rounded-2xl border border-cyan-300/30 bg-[#06171b] p-5 md:p-6 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="text-sm text-[#8fdde2]">NET SAVINGS</div>
            <div className={`text-3xl md:text-4xl font-semibold ${animatedSavings >= 0 ? "text-[#00ffff]" : "text-[#fda4af]"}`}>{money(animatedSavings)}</div>
          </div>
          <div>
            <div className="text-sm text-[#8fdde2]">ROI</div>
            <div className={`text-2xl font-semibold ${animatedRoi >= 0 ? "text-[#00ffff]" : "text-[#fda4af]"}`}>{animatedRoi.toFixed(1)}x</div>
          </div>
          <a
            href={CALENDLY_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center justify-center rounded-full bg-[#00ffff] px-5 py-2.5 text-[#081014] font-semibold hover:bg-[#77ffff] transition-colors"
          >
            Book a demo
          </a>
        </section>
      </div>
    </div>
  );
}
